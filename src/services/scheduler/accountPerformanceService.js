const redis = require('../../models/redis')
const logger = require('../../utils/logger')

class AccountPerformanceService {
  getClient() {
    return redis.getClientSafe()
  }

  getPerformanceKey(accountId) {
    return `ops:account:${accountId}:perf`
  }

  _parseInteger(value, fallback = 0) {
    const parsed = parseInt(value, 10)
    return Number.isFinite(parsed) ? parsed : fallback
  }

  _parseFloat(value, fallback = 0) {
    const parsed = parseFloat(value)
    return Number.isFinite(parsed) ? parsed : fallback
  }

  _hasPerformanceData(data = {}) {
    return Object.keys(data).length > 0
  }

  _getSuccessRate(data = {}) {
    if (data.success_rate !== undefined && data.success_rate !== null && data.success_rate !== '') {
      return Math.min(100, Math.max(0, this._parseFloat(data.success_rate, 0)))
    }

    const requests = this._parseInteger(data.requests_1h, 0)
    const errors = this._parseInteger(data.errors_1h, 0)
    if (requests <= 0) {
      return 100
    }

    return Math.min(100, Math.max(0, ((requests - errors) / requests) * 100))
  }

  _calculateScore(data = {}) {
    if (!this._hasPerformanceData(data)) {
      return 50
    }

    const successRate = this._getSuccessRate(data)
    const avgLatencyMs = this._parseFloat(data.avg_latency_ms, 0)
    const lastErrorAt = this._parseInteger(data.last_error_at, 0)

    const successRateScore = successRate * 0.5

    let latencyScore = 0
    if (avgLatencyMs < 2000) {
      latencyScore = 30
    } else if (avgLatencyMs < 5000) {
      latencyScore = 20
    } else if (avgLatencyMs < 10000) {
      latencyScore = 10
    }

    let errorRecencyScore = 20
    if (lastErrorAt > 0) {
      const elapsedMs = Date.now() - lastErrorAt
      if (elapsedMs > 3600000) {
        errorRecencyScore = 15
      } else if (elapsedMs > 600000) {
        errorRecencyScore = 10
      } else {
        errorRecencyScore = 0
      }
    }

    return Math.round(successRateScore + latencyScore + errorRecencyScore)
  }

  async getPerformanceScore(accountId) {
    if (!accountId) {
      return 50
    }

    try {
      const client = this.getClient()
      if (!client) {
        return 50
      }

      const data = await client.hgetall(this.getPerformanceKey(accountId))
      return this._calculateScore(data)
    } catch (error) {
      logger.warn(`⚠️ Failed to get performance score for account ${accountId}`, error)
      return 50
    }
  }

  async getPerformanceScores(accountIds) {
    const scores = new Map()

    if (!Array.isArray(accountIds) || accountIds.length === 0) {
      return scores
    }

    try {
      const client = this.getClient()
      if (!client) {
        accountIds.forEach((accountId) => scores.set(accountId, 50))
        return scores
      }

      const pipeline = client.pipeline()
      accountIds.forEach((accountId) => {
        pipeline.hgetall(this.getPerformanceKey(accountId))
      })

      const results = await pipeline.exec()
      accountIds.forEach((accountId, index) => {
        const [error, data] = results[index] || []
        if (error) {
          logger.warn(`⚠️ Failed to get performance score for account ${accountId}`, error)
          scores.set(accountId, 50)
          return
        }
        scores.set(accountId, this._calculateScore(data))
      })

      return scores
    } catch (error) {
      logger.warn('⚠️ Failed to get batch performance scores', error)
      accountIds.forEach((accountId) => scores.set(accountId, 50))
      return scores
    }
  }

  async shouldDeprioritize(accountId, threshold = 30) {
    const score = await this.getPerformanceScore(accountId)
    return score < threshold
  }
}

module.exports = new AccountPerformanceService()
