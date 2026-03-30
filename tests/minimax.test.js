/**
 * MiniMax Relay Service Integration Tests
 *
 * Tests cover:
 * 1. Account Service - CRUD, encryption, rate limiting, quota, model mapping
 * 2. Relay Service - Auth header (always x-api-key), request construction
 * 3. Admin Routes - REST endpoints
 * 4. Model Helper - vendor prefix parsing for 'minimax'
 * 5. Scheduler - vendor routing, account selection, rate limit dispatch
 * 6. API Key Service - account type config
 * 7. Dashboard - minimax stats aggregation
 */

// ─── Mocks ──────────────────────────────────────────────────────────────────

jest.mock('../src/utils/logger', () => ({
  api: jest.fn(),
  warn: jest.fn(),
  error: jest.fn(),
  info: jest.fn(),
  debug: jest.fn(),
  success: jest.fn(),
  database: jest.fn(),
  security: jest.fn(),
  performance: jest.fn()
}))

jest.mock('../src/models/redis', () => ({
  getClientSafe: jest.fn(),
  addToIndex: jest.fn(),
  removeFromIndex: jest.fn(),
  getAllIdsByIndex: jest.fn(),
  batchHgetallChunked: jest.fn(),
  getDateStringInTimezone: jest.fn(() => '2026-03-30'),
  getAccountUsageStats: jest.fn(() => ({
    daily: { tokens: 0, requests: 0, allTokens: 0, cost: 0 },
    total: { tokens: 0, requests: 0, allTokens: 0 },
    averages: { rpm: 0, tpm: 0 }
  })),
  isConnected: true
}))

jest.mock('../src/utils/commonHelper', () => ({
  createEncryptor: jest.fn(() => ({
    encrypt: jest.fn((data) => `encrypted:${data}`),
    decrypt: jest.fn((data) =>
      data && data.startsWith('encrypted:') ? data.replace('encrypted:', '') : data
    ),
    clearCache: jest.fn(),
    getStats: jest.fn(() => ({ cacheSize: 0 }))
  })),
  isSchedulable: jest.fn((val) => val !== false && val !== 'false')
}))

jest.mock('../src/utils/upstreamErrorHelper', () => ({
  recordErrorHistory: jest.fn(() => Promise.resolve()),
  markTempUnavailable: jest.fn(() => Promise.resolve()),
  clearTempUnavailable: jest.fn(() => Promise.resolve()),
  isTempUnavailable: jest.fn(() => Promise.resolve(false)),
  parseRetryAfter: jest.fn()
}))

jest.mock('../src/utils/proxyHelper', () => ({
  createProxyAgent: jest.fn(),
  getProxyAgent: jest.fn()
}))

jest.mock('../src/utils/webhookNotifier', () => ({
  sendAccountAnomalyNotification: jest.fn()
}))

// ─── Test Suite ─────────────────────────────────────────────────────────────

describe('MiniMax Integration', () => {
  const redis = require('../src/models/redis')
  const upstreamErrorHelper = require('../src/utils/upstreamErrorHelper')

  // ── Shared mock client ──
  let mockRedisClient
  beforeEach(() => {
    jest.clearAllMocks()
    // Restore upstreamErrorHelper mock implementations after clearAllMocks
    upstreamErrorHelper.recordErrorHistory.mockReturnValue(Promise.resolve())
    upstreamErrorHelper.markTempUnavailable.mockReturnValue(Promise.resolve())
    upstreamErrorHelper.clearTempUnavailable.mockReturnValue(Promise.resolve())
    upstreamErrorHelper.isTempUnavailable.mockReturnValue(Promise.resolve(false))
    mockRedisClient = {
      hset: jest.fn(),
      hgetall: jest.fn(),
      hmset: jest.fn(),
      hmget: jest.fn(),
      hget: jest.fn(),
      hdel: jest.fn(),
      del: jest.fn(),
      sadd: jest.fn(),
      srem: jest.fn(),
      get: jest.fn(),
      set: jest.fn(),
      expire: jest.fn(),
      ttl: jest.fn()
    }
    redis.getClientSafe.mockReturnValue(mockRedisClient)
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 1. Model Helper – vendor prefix parsing
  // ═══════════════════════════════════════════════════════════════════════════
  describe('modelHelper - minimax vendor prefix', () => {
    // Re-require to get fresh module
    const {
      parseVendorPrefixedModel,
      hasVendorPrefix,
      getVendorType,
      getEffectiveModel
    } = require('../src/utils/modelHelper')

    it('parses "minimax,MiniMax-M2" correctly', () => {
      const result = parseVendorPrefixedModel('minimax,MiniMax-M2')
      expect(result.vendor).toBe('minimax')
      expect(result.baseModel).toBe('MiniMax-M2')
    })

    it('parses "minimax,MiniMax-M2.7-highspeed" correctly', () => {
      const result = parseVendorPrefixedModel('minimax,MiniMax-M2.7-highspeed')
      expect(result.vendor).toBe('minimax')
      expect(result.baseModel).toBe('MiniMax-M2.7-highspeed')
    })

    it('hasVendorPrefix returns true for minimax prefix', () => {
      expect(hasVendorPrefix('minimax,SomeModel')).toBe(true)
    })

    it('getVendorType returns minimax', () => {
      expect(getVendorType('minimax,SomeModel')).toBe('minimax')
    })

    it('getEffectiveModel strips minimax prefix', () => {
      expect(getEffectiveModel('minimax,MiniMax-M2.5')).toBe('MiniMax-M2.5')
    })

    it('does not match minimax when not a prefix', () => {
      const result = parseVendorPrefixedModel('MiniMax-M2')
      expect(result.vendor).toBeNull()
      expect(result.baseModel).toBe('MiniMax-M2')
    })

    it('is case-insensitive for prefix', () => {
      const result = parseVendorPrefixedModel('MINIMAX,MiniMax-M2')
      expect(result.vendor).toBe('minimax')
      expect(result.baseModel).toBe('MiniMax-M2')
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 2. Account Service
  // ═══════════════════════════════════════════════════════════════════════════
  describe('MinimaxAccountService', () => {
    let minimaxAccountService

    beforeEach(() => {
      jest.resetModules()
      // Re-mock after resetModules
      jest.mock('../src/utils/logger', () => ({
        api: jest.fn(),
        warn: jest.fn(),
        error: jest.fn(),
        info: jest.fn(),
        debug: jest.fn(),
        success: jest.fn(),
        database: jest.fn(),
        security: jest.fn(),
        performance: jest.fn()
      }))
      jest.mock('../src/models/redis', () => ({
        getClientSafe: jest.fn(() => mockRedisClient),
        addToIndex: jest.fn(),
        removeFromIndex: jest.fn(),
        getAllIdsByIndex: jest.fn(),
        batchHgetallChunked: jest.fn(),
        getDateStringInTimezone: jest.fn(() => '2026-03-30'),
        getAccountUsageStats: jest.fn(() => ({
          daily: { tokens: 0, requests: 0, allTokens: 0, cost: 0 },
          total: { tokens: 0, requests: 0, allTokens: 0 },
          averages: { rpm: 0, tpm: 0 }
        }))
      }))
      jest.mock('../src/utils/commonHelper', () => ({
        createEncryptor: jest.fn(() => ({
          encrypt: jest.fn((data) => `encrypted:${data}`),
          decrypt: jest.fn((data) =>
            data && data.startsWith('encrypted:') ? data.replace('encrypted:', '') : data
          ),
          clearCache: jest.fn(),
          getStats: jest.fn(() => ({ cacheSize: 0 }))
        })),
        isSchedulable: jest.fn((val) => val !== false && val !== 'false')
      }))
      jest.mock('../src/utils/upstreamErrorHelper', () => ({
        recordErrorHistory: jest.fn(() => Promise.resolve()),
        markTempUnavailable: jest.fn(() => Promise.resolve()),
        clearTempUnavailable: jest.fn(() => Promise.resolve()),
        isTempUnavailable: jest.fn(() => Promise.resolve(false)),
        parseRetryAfter: jest.fn()
      }))
      jest.mock('../src/utils/proxyHelper', () => ({
        createProxyAgent: jest.fn(),
        getProxyAgent: jest.fn()
      }))
      jest.mock('../src/utils/webhookNotifier', () => ({
        sendAccountAnomalyNotification: jest.fn()
      }))

      minimaxAccountService = require('../src/services/account/minimaxAccountService')
    })

    it('has correct Redis key prefix', () => {
      expect(minimaxAccountService.ACCOUNT_KEY_PREFIX).toBe('minimax_account:')
    })

    it('has correct shared accounts key', () => {
      expect(minimaxAccountService.SHARED_ACCOUNTS_KEY).toBe('shared_minimax_accounts')
    })

    describe('createAccount', () => {
      it('creates account with correct defaults', async () => {
        const result = await minimaxAccountService.createAccount({
          apiUrl: 'https://api.minimax.io/anthropic',
          apiKey: 'test-key-123'
        })

        expect(result.name).toBe('MiniMax Account')
        expect(result.status).toBe('active')
        expect(result.apiUrl).toBe('https://api.minimax.io/anthropic')
        expect(result.id).toBeDefined()

        // Verify Redis calls
        const redisModule = require('../src/models/redis')
        expect(redisModule.addToIndex).toHaveBeenCalledWith('minimax_account:index', result.id)
        expect(mockRedisClient.hset).toHaveBeenCalledWith(
          `minimax_account:${result.id}`,
          expect.objectContaining({
            platform: 'minimax',
            apiUrl: 'https://api.minimax.io/anthropic',
            apiKey: expect.stringContaining('encrypted:')
          })
        )
      })

      it('throws when apiUrl or apiKey missing', async () => {
        await expect(minimaxAccountService.createAccount({ apiUrl: '' })).rejects.toThrow(
          'API URL and API Key are required for MiniMax account'
        )
      })

      it('adds shared accounts to shared set', async () => {
        const result = await minimaxAccountService.createAccount({
          apiUrl: 'https://api.minimax.io/anthropic',
          apiKey: 'key',
          accountType: 'shared'
        })

        expect(mockRedisClient.sadd).toHaveBeenCalledWith('shared_minimax_accounts', result.id)
      })

      it('does not add dedicated accounts to shared set', async () => {
        await minimaxAccountService.createAccount({
          apiUrl: 'https://api.minimax.io/anthropic',
          apiKey: 'key',
          accountType: 'dedicated'
        })

        expect(mockRedisClient.sadd).not.toHaveBeenCalled()
      })
    })

    describe('getAccount', () => {
      it('returns null when account not found', async () => {
        mockRedisClient.hgetall.mockResolvedValue({})
        const result = await minimaxAccountService.getAccount('nonexistent')
        expect(result).toBeNull()
      })

      it('decrypts apiKey and parses fields', async () => {
        mockRedisClient.hgetall.mockResolvedValue({
          id: 'test-id',
          platform: 'minimax',
          name: 'Test MiniMax',
          apiUrl: 'https://api.minimax.io/anthropic',
          apiKey: 'encrypted:sk-test',
          priority: '80',
          supportedModels: '{"MiniMax-M2":"MiniMax-M2"}',
          rateLimitDuration: '30',
          isActive: 'true',
          schedulable: 'true',
          disableAutoProtection: 'false'
        })

        const result = await minimaxAccountService.getAccount('test-id')
        expect(result.apiKey).toBe('sk-test')
        expect(result.priority).toBe(80)
        expect(result.isActive).toBe(true)
        expect(result.supportedModels).toEqual({ 'MiniMax-M2': 'MiniMax-M2' })
        expect(result.rateLimitDuration).toBe(30)
      })
    })

    describe('model mapping', () => {
      it('isModelSupported returns true for empty mapping', () => {
        expect(minimaxAccountService.isModelSupported({}, 'anything')).toBe(true)
      })

      it('isModelSupported does exact match', () => {
        const mapping = { 'MiniMax-M2': 'MiniMax-M2', 'MiniMax-M2.7': 'MiniMax-M2.7' }
        expect(minimaxAccountService.isModelSupported(mapping, 'MiniMax-M2')).toBe(true)
        expect(minimaxAccountService.isModelSupported(mapping, 'MiniMax-M3')).toBe(false)
      })

      it('isModelSupported does case-insensitive match', () => {
        const mapping = { 'MiniMax-M2': 'MiniMax-M2' }
        expect(minimaxAccountService.isModelSupported(mapping, 'minimax-m2')).toBe(true)
      })

      it('getMappedModel returns mapped name', () => {
        const mapping = { 'claude-sonnet-4-6': 'MiniMax-M2.7' }
        expect(minimaxAccountService.getMappedModel(mapping, 'claude-sonnet-4-6')).toBe(
          'MiniMax-M2.7'
        )
      })

      it('getMappedModel returns original when no mapping', () => {
        expect(minimaxAccountService.getMappedModel({}, 'MiniMax-M2')).toBe('MiniMax-M2')
      })
    })

    describe('markAccountRateLimited', () => {
      it('marks account as rate limited', async () => {
        mockRedisClient.hgetall.mockResolvedValue({
          id: 'acc-1',
          name: 'Test',
          isActive: 'true',
          rateLimitDuration: '60',
          disableAutoProtection: 'false',
          supportedModels: '{}',
          priority: '50',
          schedulable: 'true'
        })

        const result = await minimaxAccountService.markAccountRateLimited('acc-1')
        expect(result.success).toBe(true)
        expect(mockRedisClient.hmset).toHaveBeenCalledWith(
          'minimax_account:acc-1',
          expect.objectContaining({
            status: 'rate_limited',
            rateLimitStatus: 'active'
          })
        )
      })

      it('skips when auto-protection disabled', async () => {
        mockRedisClient.hgetall.mockResolvedValue({
          id: 'acc-1',
          name: 'Test',
          isActive: 'true',
          rateLimitDuration: '60',
          disableAutoProtection: 'true',
          supportedModels: '{}',
          priority: '50',
          schedulable: 'true'
        })

        const result = await minimaxAccountService.markAccountRateLimited('acc-1')
        expect(result.skipped).toBe(true)
      })
    })

    describe('markAccountOverloaded', () => {
      it('marks account as overloaded', async () => {
        mockRedisClient.hgetall.mockResolvedValue({
          id: 'acc-1',
          name: 'Test',
          isActive: 'true',
          disableAutoProtection: 'false',
          supportedModels: '{}',
          priority: '50',
          schedulable: 'true',
          rateLimitDuration: '60'
        })

        const result = await minimaxAccountService.markAccountOverloaded('acc-1')
        expect(result.success).toBe(true)
        expect(mockRedisClient.hmset).toHaveBeenCalledWith(
          'minimax_account:acc-1',
          expect.objectContaining({ status: 'overloaded' })
        )
      })
    })

    describe('isAccountRateLimited', () => {
      it('returns true when within rate limit window', async () => {
        const tenMinutesAgo = new Date(Date.now() - 10 * 60 * 1000).toISOString()
        mockRedisClient.hmget.mockResolvedValue([tenMinutesAgo, '60'])

        const result = await minimaxAccountService.isAccountRateLimited('acc-1')
        expect(result).toBe(true)
      })

      it('returns false when rate limit expired', async () => {
        const twoHoursAgo = new Date(Date.now() - 120 * 60 * 1000).toISOString()
        mockRedisClient.hmget.mockResolvedValue([twoHoursAgo, '60'])
        // removeAccountRateLimit will be called; mock hdel + hmset
        mockRedisClient.hmget.mockResolvedValueOnce([twoHoursAgo, '60'])
        mockRedisClient.hmget.mockResolvedValueOnce(['', ''])

        const result = await minimaxAccountService.isAccountRateLimited('acc-1')
        expect(result).toBe(false)
      })

      it('returns false when never rate limited', async () => {
        mockRedisClient.hmget.mockResolvedValue([null, '60'])
        const result = await minimaxAccountService.isAccountRateLimited('acc-1')
        expect(result).toBe(false)
      })
    })

    describe('isSubscriptionExpired', () => {
      it('returns false when no expiry set', () => {
        expect(minimaxAccountService.isSubscriptionExpired({})).toBe(false)
      })

      it('returns true when expired', () => {
        const yesterday = new Date(Date.now() - 86400000).toISOString()
        expect(
          minimaxAccountService.isSubscriptionExpired({ subscriptionExpiresAt: yesterday })
        ).toBe(true)
      })

      it('returns false when not expired', () => {
        const tomorrow = new Date(Date.now() + 86400000).toISOString()
        expect(
          minimaxAccountService.isSubscriptionExpired({ subscriptionExpiresAt: tomorrow })
        ).toBe(false)
      })
    })

    describe('deleteAccount', () => {
      it('deletes account and removes from index', async () => {
        mockRedisClient.del.mockResolvedValue(1)
        const redisModule = require('../src/models/redis')

        const result = await minimaxAccountService.deleteAccount('acc-1')
        expect(result.success).toBe(true)
        expect(mockRedisClient.srem).toHaveBeenCalledWith('shared_minimax_accounts', 'acc-1')
        expect(redisModule.removeFromIndex).toHaveBeenCalledWith('minimax_account:index', 'acc-1')
        expect(mockRedisClient.del).toHaveBeenCalledWith('minimax_account:acc-1')
      })

      it('throws when account not found', async () => {
        mockRedisClient.del.mockResolvedValue(0)
        await expect(minimaxAccountService.deleteAccount('nonexistent')).rejects.toThrow(
          'MiniMax Account not found or already deleted'
        )
      })
    })

    describe('resetAccountStatus', () => {
      it('resets all error fields', async () => {
        mockRedisClient.hgetall.mockResolvedValue({
          id: 'acc-1',
          name: 'Test',
          isActive: 'true',
          supportedModels: '{}',
          priority: '50',
          schedulable: 'true',
          rateLimitDuration: '60',
          disableAutoProtection: 'false',
          status: 'rate_limited'
        })

        const result = await minimaxAccountService.resetAccountStatus('acc-1')
        expect(result.success).toBe(true)
        expect(mockRedisClient.hset).toHaveBeenCalledWith(
          'minimax_account:acc-1',
          expect.objectContaining({
            status: 'active',
            errorMessage: '',
            schedulable: 'true',
            isActive: 'true'
          })
        )
        expect(mockRedisClient.hdel).toHaveBeenCalledWith(
          'minimax_account:acc-1',
          'rateLimitedAt',
          'rateLimitStatus',
          'unauthorizedAt',
          'unauthorizedCount',
          'overloadedAt',
          'overloadStatus',
          'blockedAt',
          'quotaStoppedAt'
        )
      })
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 3. API Key Service – MiniMax type config
  // ═══════════════════════════════════════════════════════════════════════════
  describe('apiKeyService - minimax config', () => {
    it('ACCOUNT_TYPE_CONFIG includes minimax', () => {
      // Read the source directly to check the config
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/services/apiKeyService'), 'utf8')
      expect(source).toContain("minimax: { prefix: 'minimax_account:' }")
    })

    it('ACCOUNT_CATEGORY_MAP includes minimax', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/services/apiKeyService'), 'utf8')
      expect(source).toContain("minimax: 'minimax'")
    })

    it('opusAccountTypes includes minimax', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/services/apiKeyService'), 'utf8')
      expect(source).toMatch(/opusAccountTypes.*=.*\[.*'minimax'/)
    })

    it('fieldMap includes minimax: null', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/services/apiKeyService'), 'utf8')
      expect(source).toContain('minimax: null')
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 4. Relay Service – Auth header
  // ═══════════════════════════════════════════════════════════════════════════
  describe('MinimaxRelayService - auth header', () => {
    it('source always uses x-api-key (never Bearer conditional)', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/relay/minimaxRelayService'),
        'utf8'
      )

      // Should have x-api-key assignment
      expect(source).toContain("requestConfig.headers['x-api-key'] = account.apiKey")

      // Should NOT have the conditional sk-ant check that CCR has
      expect(source).not.toContain("account.apiKey.startsWith('sk-ant-')")
      expect(source).not.toContain('Authorization')
    })

    it('_updateLastUsedTime uses minimax_account: prefix', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/relay/minimaxRelayService'),
        'utf8'
      )
      expect(source).toContain('`minimax_account:${accountId}`')
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 5. Admin Routes – structure check
  // ═══════════════════════════════════════════════════════════════════════════
  describe('Admin Routes', () => {
    it('index.js mounts minimax-accounts route', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/index'), 'utf8')
      expect(source).toContain("require('./minimaxAccounts')")
      expect(source).toContain("'/minimax-accounts'")
    })

    it('minimaxAccounts.js test endpoint defaults to MiniMax-M2', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/minimaxAccounts'), 'utf8')
      expect(source).toContain("model = 'MiniMax-M2'")
    })

    it('minimaxAccounts.js test uses x-api-key auth', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/minimaxAccounts'), 'utf8')
      expect(source).toContain("'x-api-key': account.apiKey")
    })

    it('minimaxAccounts.js uses default base URL for test', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/minimaxAccounts'), 'utf8')
      expect(source).toContain("'https://api.minimax.io/anthropic'")
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 6. Scheduler – MiniMax integration points
  // ═══════════════════════════════════════════════════════════════════════════
  describe('Scheduler - minimax integration', () => {
    it('imports minimaxAccountService', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain("require('../account/minimaxAccountService')")
    })

    it('has minimax model support check in _isModelSupportedByAccount', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain("accountType === 'minimax' && account.supportedModels")
    })

    it('has minimax vendor prefix routing in selectAccountForApiKey', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain("vendor === 'minimax'")
      expect(source).toContain('this._selectMinimaxAccount')
    })

    it('has includeMinimax param in _getAllAvailableAccounts', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('includeMinimax = false')
    })

    it('has minimax block in _isAccountAvailable', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain("} else if (accountType === 'minimax') {")
    })

    it('dispatches minimax in markAccountRateLimited', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('minimaxAccountService.markAccountRateLimited(accountId)')
    })

    it('dispatches minimax in removeAccountRateLimit', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('minimaxAccountService.removeAccountRateLimit(accountId)')
    })

    it('dispatches minimax in isAccountRateLimited', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('minimaxAccountService.isAccountRateLimited(accountId)')
    })

    it('has allowMinimax param in selectAccountFromGroup', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('allowMinimax = false')
    })

    it('has _selectMinimaxAccount method', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('async _selectMinimaxAccount(')
    })

    it('has _getAvailableMinimaxAccounts method', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('async _getAvailableMinimaxAccounts(')
    })

    it('filters minimax sticky sessions when allowMinimax is false', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain("!allowMinimax && mappedAccount.accountType === 'minimax'")
    })

    it('tries minimax accounts in group member resolution', () => {
      const fs = require('fs')
      const source = fs.readFileSync(
        require.resolve('../src/services/scheduler/unifiedClaudeScheduler'),
        'utf8'
      )
      expect(source).toContain('minimaxAccountService.getAccount(memberId)')
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 7. API Route – dispatch blocks
  // ═══════════════════════════════════════════════════════════════════════════
  describe('API Route - minimax dispatch', () => {
    it('imports minimaxRelayService', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/api'), 'utf8')
      expect(source).toContain("require('../services/relay/minimaxRelayService')")
    })

    it('has minimax stream dispatch block', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/api'), 'utf8')
      expect(source).toContain('minimaxRelayService.relayStreamRequestWithUsageCapture')
      expect(source).toContain("'minimax-stream'")
    })

    it('has minimax non-stream dispatch block', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/api'), 'utf8')
      expect(source).toContain('minimaxRelayService.relayRequest(')
    })

    it('records usage as minimax account type', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/api'), 'utf8')
      // The stream block should record with 'minimax' account type
      const minimaxStreamSection = source.substring(
        source.indexOf('MiniMax usage callback triggered')
      )
      expect(minimaxStreamSection).toContain("'minimax'")
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 8. Dashboard – minimax stats
  // ═══════════════════════════════════════════════════════════════════════════
  describe('Dashboard - minimax integration', () => {
    it('imports minimaxAccountService', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/dashboard'), 'utf8')
      expect(source).toContain("require('../../services/account/minimaxAccountService')")
    })

    it('fetches minimax accounts in Promise.all', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/dashboard'), 'utf8')
      expect(source).toContain('minimaxAccountService.getAllAccounts()')
    })

    it('computes minimaxStats', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/dashboard'), 'utf8')
      expect(source).toContain('const minimaxStats = countAccountStats(minimaxAccounts)')
    })

    it('includes minimax in totalAccounts', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/dashboard'), 'utf8')
      expect(source).toContain('minimaxAccounts.length')
    })

    it('includes minimax in accountsByPlatform', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/dashboard'), 'utf8')
      expect(source).toContain('minimax: {')
      expect(source).toContain('minimaxStats.normal')
    })

    it('includes minimax in activeAccounts aggregate', () => {
      const fs = require('fs')
      const source = fs.readFileSync(require.resolve('../src/routes/admin/dashboard'), 'utf8')
      // Check the activeAccounts sum includes minimax
      const activeAccountsSection = source.substring(
        source.indexOf('activeAccounts:'),
        source.indexOf('totalClaudeAccounts:')
      )
      expect(activeAccountsSection).toContain('minimaxStats.normal')
    })
  })

  // ═══════════════════════════════════════════════════════════════════════════
  // 9. Comprehensive model processing tests
  // ═══════════════════════════════════════════════════════════════════════════
  describe('MinimaxAccountService - _processModelMapping', () => {
    let minimaxAccountService

    beforeEach(() => {
      jest.resetModules()
      jest.mock('../src/utils/logger', () => ({
        api: jest.fn(),
        warn: jest.fn(),
        error: jest.fn(),
        info: jest.fn(),
        debug: jest.fn(),
        success: jest.fn(),
        database: jest.fn(),
        security: jest.fn(),
        performance: jest.fn()
      }))
      jest.mock('../src/models/redis', () => ({
        getClientSafe: jest.fn(() => mockRedisClient),
        addToIndex: jest.fn(),
        removeFromIndex: jest.fn(),
        getAllIdsByIndex: jest.fn(),
        batchHgetallChunked: jest.fn(),
        getDateStringInTimezone: jest.fn(() => '2026-03-30'),
        getAccountUsageStats: jest.fn(() => ({
          daily: { tokens: 0, requests: 0, allTokens: 0, cost: 0 },
          total: { tokens: 0, requests: 0, allTokens: 0 },
          averages: { rpm: 0, tpm: 0 }
        }))
      }))
      jest.mock('../src/utils/commonHelper', () => ({
        createEncryptor: jest.fn(() => ({
          encrypt: jest.fn((data) => `encrypted:${data}`),
          decrypt: jest.fn((data) =>
            data && data.startsWith('encrypted:') ? data.replace('encrypted:', '') : data
          ),
          clearCache: jest.fn(),
          getStats: jest.fn(() => ({ cacheSize: 0 }))
        })),
        isSchedulable: jest.fn((val) => val !== false && val !== 'false')
      }))
      jest.mock('../src/utils/upstreamErrorHelper', () => ({
        recordErrorHistory: jest.fn(() => Promise.resolve()),
        markTempUnavailable: jest.fn(() => Promise.resolve()),
        clearTempUnavailable: jest.fn(() => Promise.resolve()),
        isTempUnavailable: jest.fn(() => Promise.resolve(false)),
        parseRetryAfter: jest.fn()
      }))
      jest.mock('../src/utils/proxyHelper', () => ({
        createProxyAgent: jest.fn(),
        getProxyAgent: jest.fn()
      }))

      minimaxAccountService = require('../src/services/account/minimaxAccountService')
    })

    it('converts empty array to empty object', () => {
      expect(minimaxAccountService._processModelMapping([])).toEqual({})
    })

    it('converts null to empty object', () => {
      expect(minimaxAccountService._processModelMapping(null)).toEqual({})
    })

    it('passes through object mapping', () => {
      const mapping = { 'MiniMax-M2': 'MiniMax-M2' }
      expect(minimaxAccountService._processModelMapping(mapping)).toEqual(mapping)
    })

    it('converts array to identity mapping', () => {
      const result = minimaxAccountService._processModelMapping(['MiniMax-M2', 'MiniMax-M2.7'])
      expect(result).toEqual({
        'MiniMax-M2': 'MiniMax-M2',
        'MiniMax-M2.7': 'MiniMax-M2.7'
      })
    })
  })
})
