/**
 * 对话日志管理路由
 * 查询和管理 API 调用的对话内容日志
 */

const express = require('express')
const { authenticateAdmin } = require('../../middleware/auth')
const conversationLogService = require('../../services/conversationLogService')
const logger = require('../../utils/logger')

const router = express.Router()

/**
 * GET /admin/conversation-logs/:keyId
 * 查询某个 API Key 的对话日志列表
 * Query params: ?limit=20&sessionHash=xxx
 */
router.get('/conversation-logs/:keyId', authenticateAdmin, async (req, res) => {
  try {
    const { keyId } = req.params
    const limit = parseInt(req.query.limit) || 20
    const sessionHash = req.query.sessionHash || undefined

    const logs = await conversationLogService.getConversationLogs(keyId, { sessionHash, limit })

    return res.json({
      success: true,
      data: logs,
      total: logs.length
    })
  } catch (error) {
    logger.error('Failed to get conversation logs:', error)
    return res.status(500).json({
      error: 'Failed to get conversation logs',
      message: error.message
    })
  }
})

/**
 * DELETE /admin/conversation-logs/:keyId
 * 清空某个 API Key 的所有对话日志
 */
router.delete('/conversation-logs/:keyId', authenticateAdmin, async (req, res) => {
  try {
    const { keyId } = req.params
    const deletedCount = await conversationLogService.deleteConversationLogs(keyId)

    return res.json({
      success: true,
      deletedCount
    })
  } catch (error) {
    logger.error('Failed to delete conversation logs:', error)
    return res.status(500).json({
      error: 'Failed to delete conversation logs',
      message: error.message
    })
  }
})

module.exports = router
