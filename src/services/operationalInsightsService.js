const redis = require('../models/redis')
const logger = require('../utils/logger')

class OperationalInsightsService {
  constructor() {
    this.HOURLY_PREFIX = 'ops:hourly:'
    this.ACCOUNT_PERF_PREFIX = 'ops:account:'
    this.ERRORS_PREFIX = 'ops:errors:'
    this.HOURLY_TTL_SECONDS = 72 * 3600
    this.ACCOUNT_TTL_SECONDS = 24 * 3600
    this.HOURLY_FIELDS = [
      'requests',
      'completions',
      'errors',
      'avg_latency_ms',
      'sticky_hits',
      'sticky_misses',
      'pool_selections',
      'group_selections',
      'dedicated_selections',
      'disconnects',
      'total_input_tokens',
      'total_output_tokens'
    ]
    this.SELECTION_FIELD_MAP = {
      dedicated: 'dedicated_selections',
      group: 'group_selections',
      pool: 'pool_selections'
    }
  }

  getClient() {
    return redis.getClientSafe()
  }

  getHourKey(date = new Date()) {
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hour = String(date.getHours()).padStart(2, '0')
    return `${year}-${month}-${day}-${hour}`
  }

  getHourlyRedisKey(date = new Date()) {
    return `${this.HOURLY_PREFIX}${this.getHourKey(date)}`
  }

  getErrorsRedisKey(date = new Date()) {
    return `${this.ERRORS_PREFIX}${this.getHourKey(date)}`
  }

  getAccountPerfKey(accountId) {
    return `${this.ACCOUNT_PERF_PREFIX}${accountId}:perf`
  }

  parseInteger(value) {
    const parsed = parseInt(value || 0, 10)
    return Number.isFinite(parsed) ? parsed : 0
  }

  parseFloatValue(value) {
    const parsed = parseFloat(value || 0)
    return Number.isFinite(parsed) ? parsed : 0
  }

  normalizeHourlyMetrics(hour, data = {}) {
    const result = {
      hour,
      requests: this.parseInteger(data.requests),
      completions: this.parseInteger(data.completions),
      errors: this.parseInteger(data.errors),
      avg_latency_ms: this.parseFloatValue(data.avg_latency_ms),
      sticky_hits: this.parseInteger(data.sticky_hits),
      sticky_misses: this.parseInteger(data.sticky_misses),
      pool_selections: this.parseInteger(data.pool_selections),
      group_selections: this.parseInteger(data.group_selections),
      dedicated_selections: this.parseInteger(data.dedicated_selections),
      disconnects: this.parseInteger(data.disconnects),
      total_input_tokens: this.parseInteger(data.total_input_tokens),
      total_output_tokens: this.parseInteger(data.total_output_tokens)
    }

    const completions = this.parseInteger(data.completions)
    const latencyTotal = this.parseFloatValue(data.latency_total_ms)
    if (!result.avg_latency_ms && completions > 0 && latencyTotal > 0) {
      result.avg_latency_ms = latencyTotal / completions
    }

    return result
  }

  normalizeAccountPerformance(accountId, data = {}) {
    const requests = this.parseInteger(data.requests_1h)
    const errors = this.parseInteger(data.errors_1h)
    const latencyTotal = this.parseFloatValue(data.latency_total_ms)
    const latencySamples = this.parseInteger(data.latency_samples)
    const successRate =
      requests > 0 ? Number((((requests - errors) / requests) * 100).toFixed(2)) : 100

    return {
      accountId,
      requests_1h: requests,
      errors_1h: errors,
      avg_latency_ms:
        latencySamples > 0 && latencyTotal > 0
          ? Number((latencyTotal / latencySamples).toFixed(2))
          : this.parseFloatValue(data.avg_latency_ms),
      last_error_code: data.last_error_code || null,
      last_error_at: data.last_error_at || null,
      success_rate:
        data.success_rate !== undefined ? this.parseFloatValue(data.success_rate) : successRate
    }
  }

  async setHourlyAverageLatency(hourlyKey, latencyMs) {
    const client = this.getClient()
    const current = await client.hmget(hourlyKey, 'latency_total_ms', 'completions')
    const totalLatency = this.parseFloatValue(current[0]) + this.parseFloatValue(latencyMs)
    const completions = this.parseInteger(current[1])
    const average = completions > 0 ? totalLatency / completions : this.parseFloatValue(latencyMs)

    const pipeline = client.pipeline()
    pipeline.hincrbyfloat(hourlyKey, 'latency_total_ms', this.parseFloatValue(latencyMs))
    pipeline.hset(hourlyKey, 'avg_latency_ms', average.toFixed(2))
    pipeline.expire(hourlyKey, this.HOURLY_TTL_SECONDS)
    await pipeline.exec()
  }

  async updateAccountPerformance(accountId, updates = {}) {
    if (!accountId) {
      return
    }

    const client = this.getClient()
    const accountKey = this.getAccountPerfKey(accountId)
    const pipeline = client.pipeline()

    if (updates.requests) {
      pipeline.hincrby(accountKey, 'requests_1h', updates.requests)
    }

    if (updates.errors) {
      pipeline.hincrby(accountKey, 'errors_1h', updates.errors)
    }

    if (updates.latencyMs) {
      pipeline.hincrbyfloat(accountKey, 'latency_total_ms', this.parseFloatValue(updates.latencyMs))
      pipeline.hincrby(accountKey, 'latency_samples', 1)
    }

    if (updates.lastErrorCode) {
      pipeline.hset(accountKey, 'last_error_code', updates.lastErrorCode)
    }

    if (updates.lastErrorAt) {
      pipeline.hset(accountKey, 'last_error_at', updates.lastErrorAt)
    }

    pipeline.expire(accountKey, this.ACCOUNT_TTL_SECONDS)
    await pipeline.exec()

    const current = await client.hmget(
      accountKey,
      'requests_1h',
      'errors_1h',
      'latency_total_ms',
      'latency_samples'
    )
    const requests = this.parseInteger(current[0])
    const errors = this.parseInteger(current[1])
    const latencyTotal = this.parseFloatValue(current[2])
    const latencySamples = this.parseInteger(current[3])
    const avgLatency = latencySamples > 0 ? latencyTotal / latencySamples : 0
    const successRate = requests > 0 ? ((requests - errors) / requests) * 100 : 100

    await client
      .pipeline()
      .hset(accountKey, 'avg_latency_ms', avgLatency.toFixed(2))
      .hset(accountKey, 'success_rate', successRate.toFixed(2))
      .expire(accountKey, this.ACCOUNT_TTL_SECONDS)
      .exec()
  }

  async recordRequest(requestId, apiKeyId, apiKeyName) {
    try {
      const client = this.getClient()
      const hourlyKey = this.getHourlyRedisKey()
      await client
        .pipeline()
        .hincrby(hourlyKey, 'requests', 1)
        .expire(hourlyKey, this.HOURLY_TTL_SECONDS)
        .exec()
    } catch (error) {
      logger.error('Failed to record operational request metric:', {
        requestId,
        apiKeyId,
        apiKeyName,
        error: error.message
      })
    }
  }

  async recordSchedulerDecision(requestId, decision = {}) {
    try {
      const {
        accountId,
        accountType,
        selectionMethod,
        groupId,
        stickyHit,
        candidateCount,
        vendor
      } = decision
      const client = this.getClient()
      const hourlyKey = this.getHourlyRedisKey()
      const pipeline = client.pipeline()

      const selectionField = this.SELECTION_FIELD_MAP[selectionMethod]
      if (selectionField) {
        pipeline.hincrby(hourlyKey, selectionField, 1)
      }

      if (selectionMethod === 'sticky' && stickyHit === false) {
        pipeline.hincrby(hourlyKey, 'sticky_misses', 1)
      } else if (stickyHit === true) {
        pipeline.hincrby(hourlyKey, 'sticky_hits', 1)
      }

      pipeline.expire(hourlyKey, this.HOURLY_TTL_SECONDS)
      await pipeline.exec()

      await this.updateAccountPerformance(accountId, { requests: 1 })

      logger.debug('Recorded operational scheduler decision', {
        requestId,
        accountId,
        accountType,
        selectionMethod,
        groupId,
        stickyHit,
        candidateCount,
        vendor
      })
    } catch (error) {
      logger.error('Failed to record scheduler decision metric:', {
        requestId,
        error: error.message
      })
    }
  }

  async recordCompletion(requestId, completion = {}) {
    try {
      const {
        accountId,
        accountType,
        latencyMs,
        inputTokens,
        outputTokens,
        statusCode,
        isStreaming,
        wasDisconnected
      } = completion

      const client = this.getClient()
      const hourlyKey = this.getHourlyRedisKey()
      const pipeline = client.pipeline()

      pipeline.hincrby(hourlyKey, 'completions', 1)
      pipeline.hincrby(hourlyKey, 'total_input_tokens', this.parseInteger(inputTokens))
      pipeline.hincrby(hourlyKey, 'total_output_tokens', this.parseInteger(outputTokens))
      if (wasDisconnected) {
        pipeline.hincrby(hourlyKey, 'disconnects', 1)
      }
      pipeline.expire(hourlyKey, this.HOURLY_TTL_SECONDS)
      await pipeline.exec()

      if (latencyMs !== undefined && latencyMs !== null) {
        await this.setHourlyAverageLatency(hourlyKey, latencyMs)
      }

      await this.updateAccountPerformance(accountId, {
        latencyMs,
        requests: 0
      })

      logger.debug('Recorded operational completion metric', {
        requestId,
        accountId,
        accountType,
        statusCode,
        isStreaming,
        wasDisconnected
      })
    } catch (error) {
      logger.error('Failed to record completion metric:', {
        requestId,
        error: error.message
      })
    }
  }

  async recordError(requestId, errorInfo = {}) {
    try {
      const { accountId, accountType, errorCode, errorType, retried } = errorInfo
      const client = this.getClient()
      const hourlyKey = this.getHourlyRedisKey()
      const errorsKey = this.getErrorsRedisKey()
      const now = new Date().toISOString()

      await client
        .pipeline()
        .hincrby(hourlyKey, 'errors', 1)
        .expire(hourlyKey, this.HOURLY_TTL_SECONDS)
        .zincrby(errorsKey, 1, errorCode || 'unknown_error')
        .expire(errorsKey, this.HOURLY_TTL_SECONDS)
        .exec()

      await this.updateAccountPerformance(accountId, {
        errors: 1,
        lastErrorCode: errorCode || errorType || 'unknown_error',
        lastErrorAt: now
      })

      logger.debug('Recorded operational error metric', {
        requestId,
        accountId,
        accountType,
        errorCode,
        errorType,
        retried
      })
    } catch (error) {
      logger.error('Failed to record operational error metric:', {
        requestId,
        error: error.message
      })
    }
  }

  async getHourlyMetrics(hours = 24) {
    const safeHours = Math.max(1, Math.min(this.parseInteger(hours) || 24, 72))
    const client = this.getClient()
    const keys = []

    for (let offset = safeHours - 1; offset >= 0; offset -= 1) {
      const date = new Date(Date.now() - offset * 3600000)
      const hour = this.getHourKey(date)
      keys.push({
        hour,
        key: `${this.HOURLY_PREFIX}${hour}`
      })
    }

    const pipeline = client.pipeline()
    keys.forEach(({ key }) => pipeline.hgetall(key))
    const results = await pipeline.exec()

    return keys.map(({ hour }, index) => {
      const [, data] = results[index] || []
      return this.normalizeHourlyMetrics(hour, data)
    })
  }

  async getAccountPerformance(accountId) {
    if (!accountId) {
      return null
    }

    const client = this.getClient()
    const data = await client.hgetall(this.getAccountPerfKey(accountId))
    if (!data || Object.keys(data).length === 0) {
      return null
    }

    return this.normalizeAccountPerformance(accountId, data)
  }

  async getSchedulerStats(hours = 24) {
    const hourlyMetrics = await this.getHourlyMetrics(hours)
    const totals = hourlyMetrics.reduce(
      (accumulator, item) => {
        accumulator.sticky_hits += item.sticky_hits
        accumulator.sticky_misses += item.sticky_misses
        accumulator.pool_selections += item.pool_selections
        accumulator.group_selections += item.group_selections
        accumulator.dedicated_selections += item.dedicated_selections
        return accumulator
      },
      {
        sticky_hits: 0,
        sticky_misses: 0,
        pool_selections: 0,
        group_selections: 0,
        dedicated_selections: 0
      }
    )

    const stickyAttempts = totals.sticky_hits + totals.sticky_misses

    return {
      hours: Math.max(1, Math.min(this.parseInteger(hours) || 24, 72)),
      sticky_hit_rate:
        stickyAttempts > 0 ? Number(((totals.sticky_hits / stickyAttempts) * 100).toFixed(2)) : 0,
      sticky_attempts: stickyAttempts,
      selection_breakdown: {
        pool: totals.pool_selections,
        group: totals.group_selections,
        dedicated: totals.dedicated_selections,
        sticky_hits: totals.sticky_hits,
        sticky_misses: totals.sticky_misses
      }
    }
  }

  async scanAccountPerformance() {
    const client = this.getClient()
    const accounts = []
    let cursor = '0'

    do {
      const [nextCursor, keys] = await client.scan(
        cursor,
        'MATCH',
        `${this.ACCOUNT_PERF_PREFIX}*:perf`,
        'COUNT',
        200
      )
      cursor = nextCursor

      if (!keys || keys.length === 0) {
        continue
      }

      const pipeline = client.pipeline()
      keys.forEach((key) => pipeline.hgetall(key))
      const results = await pipeline.exec()

      keys.forEach((key, index) => {
        const [, data] = results[index] || []
        if (!data || Object.keys(data).length === 0) {
          return
        }

        const accountId = key.slice(this.ACCOUNT_PERF_PREFIX.length, -':perf'.length)
        accounts.push(this.normalizeAccountPerformance(accountId, data))
      })
    } while (cursor !== '0')

    accounts.sort((a, b) => b.requests_1h - a.requests_1h)
    return accounts
  }

  async getSummary() {
    const [hourlyMetrics, schedulerStats, accounts] = await Promise.all([
      this.getHourlyMetrics(24),
      this.getSchedulerStats(24),
      this.scanAccountPerformance()
    ])

    const totals = hourlyMetrics.reduce(
      (accumulator, item) => {
        accumulator.requests += item.requests
        accumulator.completions += item.completions
        accumulator.errors += item.errors
        accumulator.disconnects += item.disconnects
        accumulator.total_input_tokens += item.total_input_tokens
        accumulator.total_output_tokens += item.total_output_tokens
        accumulator.weightedLatency += item.avg_latency_ms * item.completions
        accumulator.completionCount += item.completions
        return accumulator
      },
      {
        requests: 0,
        completions: 0,
        errors: 0,
        disconnects: 0,
        total_input_tokens: 0,
        total_output_tokens: 0,
        weightedLatency: 0,
        completionCount: 0
      }
    )

    const avgLatency =
      totals.completionCount > 0 ? totals.weightedLatency / totals.completionCount : 0
    const errorRate = totals.requests > 0 ? (totals.errors / totals.requests) * 100 : 0
    const completionRate = totals.requests > 0 ? (totals.completions / totals.requests) * 100 : 0
    const unhealthyAccounts = accounts.filter((account) => account.success_rate < 95).length

    return {
      generated_at: new Date().toISOString(),
      window_hours: 24,
      requests: totals.requests,
      completions: totals.completions,
      errors: totals.errors,
      disconnects: totals.disconnects,
      total_input_tokens: totals.total_input_tokens,
      total_output_tokens: totals.total_output_tokens,
      avg_latency_ms: Number(avgLatency.toFixed(2)),
      error_rate: Number(errorRate.toFixed(2)),
      completion_rate: Number(completionRate.toFixed(2)),
      sticky_hit_rate: schedulerStats.sticky_hit_rate,
      active_accounts: accounts.length,
      unhealthy_accounts: unhealthyAccounts
    }
  }
}

module.exports = new OperationalInsightsService()
