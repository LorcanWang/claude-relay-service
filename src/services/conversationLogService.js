/**
 * 对话日志服务
 * 独立记录 API 调用的对话内容（请求消息 + 模型回复）
 * 使用独立的 Redis key 前缀 convlog:，不侵入现有 usage 记录
 */

const redis = require('../models/redis')
const logger = require('../utils/logger')
const crypto = require('crypto')

// 硬编码常量
const ENABLED = true
const MAX_RECORDS = 1 // 每 Key+Session 只保留最新一条
const TTL_DAYS = 7

class ConversationLogService {
  /**
   * 异步记录对话日志，失败不影响主流程
   */
  async logConversation({
    keyId,
    sessionHash,
    model,
    accountId,
    accountType,
    requestBody,
    responseContent,
    stopReason,
    usage
  }) {
    if (!ENABLED) {
      return
    }

    try {
      const messages = this._sanitizeMessages(requestBody?.messages)
      const systemPrompt = this._extractSystemText(requestBody?.system)
      const responseText = this._extractResponseText(responseContent)

      const record = {
        id: crypto.randomUUID(),
        timestamp: new Date().toISOString(),
        keyId,
        sessionHash: sessionHash || null,
        model: model || 'unknown',
        accountId: accountId || null,
        accountType: accountType || null,
        request: {
          system: systemPrompt,
          messages,
          messageCount: requestBody?.messages?.length || 0
        },
        response: {
          content: responseText,
          stopReason: stopReason || null
        },
        usage: {
          inputTokens: usage?.inputTokens || 0,
          outputTokens: usage?.outputTokens || 0
        }
      }

      const listKey = `convlog:${keyId}:${sessionHash || 'default'}`
      const client = redis.getClientSafe()

      await client
        .multi()
        .lpush(listKey, JSON.stringify(record))
        .ltrim(listKey, 0, Math.max(0, MAX_RECORDS - 1))
        .expire(listKey, 86400 * TTL_DAYS)
        .exec()

      logger.debug(`📝 Conversation logged for key ${keyId}, session ${sessionHash || 'default'}`)
    } catch (error) {
      logger.error('❌ Failed to log conversation:', error.message)
    }
  }

  /**
   * 净化 messages 数组，保留完整结构，base64 图片替换为占位符
   */
  _sanitizeMessages(messages) {
    if (!Array.isArray(messages)) {
      return []
    }

    return messages.map((msg) => ({
      role: msg.role,
      content: this._sanitizeContent(msg.content)
    }))
  }

  /**
   * 净化消息内容，支持字符串和 content blocks 数组
   */
  _sanitizeContent(content) {
    if (typeof content === 'string') {
      return content
    }

    if (!Array.isArray(content)) {
      return content
    }

    return content.map((block) => {
      // 图片类型：替换 base64 数据为占位符
      if (block.type === 'image' && block.source?.type === 'base64') {
        return {
          type: 'image',
          source: {
            type: 'base64',
            media_type: block.source.media_type || 'unknown',
            data: `[image: ${block.source.media_type || 'unknown'}]`
          }
        }
      }

      // 文本类型：保持原样
      if (block.type === 'text') {
        return block
      }

      // tool_use / tool_result 等其他类型：保持原样
      return block
    })
  }

  /**
   * 提取 system prompt 文本
   */
  _extractSystemText(system) {
    if (!system) {
      return null
    }
    if (typeof system === 'string') {
      return system
    }

    // system 可以是 content blocks 数组
    if (Array.isArray(system)) {
      return system
        .map((block) => {
          if (typeof block === 'string') {
            return block
          }
          if (block.type === 'text') {
            return block.text
          }
          return ''
        })
        .filter(Boolean)
        .join('\n')
    }

    return null
  }

  /**
   * 从响应内容提取文本
   * 支持 content 数组（非流式）和纯文本字符串（流式累积）
   */
  _extractResponseText(content) {
    if (!content) {
      return null
    }
    if (typeof content === 'string') {
      return content
    }

    // 非流式响应：content 是 content blocks 数组
    if (Array.isArray(content)) {
      return content
        .map((block) => {
          if (block.type === 'text') {
            return block.text
          }
          if (block.type === 'tool_use') {
            return `[tool_use: ${block.name}]`
          }
          return ''
        })
        .filter(Boolean)
        .join('\n')
    }

    return null
  }

  /**
   * 查询某个 API Key 的对话日志
   */
  async getConversationLogs(keyId, { sessionHash, limit = 20 } = {}) {
    const client = redis.getClient()
    if (!client) {
      return []
    }

    try {
      if (sessionHash) {
        // 查询特定 session 的日志
        const listKey = `convlog:${keyId}:${sessionHash}`
        const rawRecords = await client.lrange(listKey, 0, Math.max(0, limit - 1))
        return rawRecords
          .map((entry) => {
            try {
              return JSON.parse(entry)
            } catch {
              return null
            }
          })
          .filter(Boolean)
      }

      // 查询该 key 所有 session 的日志
      const pattern = `convlog:${keyId}:*`
      const keys = await client.keys(pattern)

      if (keys.length === 0) {
        return []
      }

      const results = []
      for (const key of keys) {
        const rawRecords = await client.lrange(key, 0, Math.max(0, limit - 1))
        for (const entry of rawRecords) {
          try {
            results.push(JSON.parse(entry))
          } catch {
            // 忽略解析错误
          }
        }
      }

      // 按时间倒序排列
      results.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))
      return results.slice(0, limit)
    } catch (error) {
      logger.error(`❌ Failed to get conversation logs for key ${keyId}:`, error.message)
      return []
    }
  }

  /**
   * 删除某个 API Key 的所有对话日志
   */
  async deleteConversationLogs(keyId) {
    const client = redis.getClient()
    if (!client) {
      return 0
    }

    try {
      const pattern = `convlog:${keyId}:*`
      const keys = await client.keys(pattern)

      if (keys.length === 0) {
        return 0
      }

      await client.del(...keys)
      return keys.length
    } catch (error) {
      logger.error(`❌ Failed to delete conversation logs for key ${keyId}:`, error.message)
      return 0
    }
  }
}

module.exports = new ConversationLogService()
