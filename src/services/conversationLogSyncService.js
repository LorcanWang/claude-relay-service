/**
 * 对话日志同步服务
 * 每天北京时间 23:59 将 Redis 中的对话日志同步到 Firebase Firestore
 */

const cron = require('node-cron')
const admin = require('firebase-admin')
const redis = require('../models/redis')
const logger = require('../utils/logger')
const path = require('path')

// 硬编码常量
const COLLECTION = 'conversationLogs'
const CRON_EXPRESSION = '59 23 * * *' // 每天 23:59
const TIMEZONE = 'Asia/Shanghai'
const BATCH_SIZE = 500 // Firestore 批量写入上限

// Firebase 初始化
let db = null

function initFirebase() {
  if (db) {
    return db
  }
  try {
    const serviceAccount = require(
      path.join(__dirname, '../../config/firebase-service-account.json')
    )
    if (!admin.apps.length) {
      admin.initializeApp({
        credential: admin.credential.cert(serviceAccount)
      })
    }
    db = admin.firestore()
    logger.info('🔥 Firebase Firestore initialized for conversation log sync')
    return db
  } catch (error) {
    logger.error('❌ Failed to initialize Firebase:', error.message)
    return null
  }
}

class ConversationLogSyncService {
  constructor() {
    this.cronTask = null
  }

  start() {
    const firestore = initFirebase()
    if (!firestore) {
      logger.warn('⚠️ Conversation log sync disabled: Firebase not configured')
      return
    }

    this.cronTask = cron.schedule(
      CRON_EXPRESSION,
      async () => {
        await this.syncToFirestore()
      },
      { timezone: TIMEZONE }
    )

    logger.info(`📝 Conversation log sync scheduled at ${CRON_EXPRESSION} (${TIMEZONE})`)
  }

  stop() {
    if (this.cronTask) {
      this.cronTask.stop()
      this.cronTask = null
      logger.info('📝 Conversation log sync stopped')
    }
  }

  async syncToFirestore() {
    const firestore = initFirebase()
    if (!firestore) {
      return
    }

    const client = redis.getClient()
    if (!client) {
      logger.warn('⚠️ Redis not connected, skipping conversation log sync')
      return
    }

    try {
      // 获取当天日期（北京时间）
      const now = new Date()
      const bjOffset = 8 * 3600000
      const bjDate = new Date(now.getTime() + bjOffset)
      const syncDate = bjDate.toISOString().slice(0, 10) // YYYY-MM-DD

      // 扫描所有 convlog 键
      const keys = await client.keys('convlog:*')
      if (keys.length === 0) {
        logger.info('📝 No conversation logs to sync')
        return
      }

      logger.info(`📝 Syncing ${keys.length} conversation log keys to Firestore...`)

      const records = []
      for (const key of keys) {
        try {
          const rawRecords = await client.lrange(key, 0, 0) // 只取最新一条
          if (rawRecords.length === 0) {
            continue
          }

          const record = JSON.parse(rawRecords[0])
          records.push(record)
        } catch (parseError) {
          logger.warn(`⚠️ Failed to parse conversation log from key ${key}:`, parseError.message)
        }
      }

      if (records.length === 0) {
        logger.info('📝 No valid conversation log records to sync')
        return
      }

      // 按 keyId 分组
      const grouped = {}
      for (const record of records) {
        const keyId = record.keyId || 'unknown'
        if (!grouped[keyId]) {
          grouped[keyId] = {}
        }
        const sessionHash = (record.sessionHash || 'default').slice(0, 16)
        const fieldKey = `${sessionHash}_${syncDate}`
        grouped[keyId][fieldKey] = {
          ...record,
          syncedAt: new Date().toISOString()
        }
      }

      // 按 keyId 写入 Firestore（每个 keyId 一个文档，merge 模式）
      let synced = 0
      const keyIds = Object.keys(grouped)
      for (let i = 0; i < keyIds.length; i += BATCH_SIZE) {
        const chunk = keyIds.slice(i, i + BATCH_SIZE)
        const batch = firestore.batch()

        for (const keyId of chunk) {
          const docRef = firestore.collection(COLLECTION).doc(keyId)
          batch.set(docRef, grouped[keyId], { merge: true })
          synced += Object.keys(grouped[keyId]).length
        }

        await batch.commit()
      }

      logger.info(
        `📝 Synced ${synced} conversation logs to Firestore across ${keyIds.length} keys (date: ${syncDate})`
      )
    } catch (error) {
      logger.error('❌ Failed to sync conversation logs to Firestore:', error.message)
    }
  }
}

module.exports = new ConversationLogSyncService()
