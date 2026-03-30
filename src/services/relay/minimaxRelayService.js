const axios = require('axios')
const minimaxAccountService = require('../account/minimaxAccountService')
const logger = require('../../utils/logger')
const config = require('../../../config/config')
const { parseVendorPrefixedModel } = require('../../utils/modelHelper')
const userMessageQueueService = require('../userMessageQueueService')
const { isStreamWritable } = require('../../utils/streamHelper')
const upstreamErrorHelper = require('../../utils/upstreamErrorHelper')

class MinimaxRelayService {
  constructor() {
    this.defaultUserAgent = 'claude-relay-service/1.0.0'
  }

  // 🚀 转发请求到MiniMax API
  async relayRequest(
    requestBody,
    apiKeyData,
    clientRequest,
    clientResponse,
    clientHeaders,
    accountId,
    options = {}
  ) {
    let abortController = null
    let account = null
    let queueLockAcquired = false
    let queueRequestId = null

    try {
      // 📬 用户消息队列处理
      if (userMessageQueueService.isUserMessageRequest(requestBody)) {
        // 校验 accountId 非空，避免空值污染队列锁键
        if (!accountId || accountId === '') {
          logger.error('❌ accountId missing for queue lock in MiniMax relayRequest')
          throw new Error('accountId missing for queue lock')
        }
        const queueResult = await userMessageQueueService.acquireQueueLock(accountId)
        if (!queueResult.acquired && !queueResult.skipped) {
          // 区分 Redis 后端错误和队列超时
          const isBackendError = queueResult.error === 'queue_backend_error'
          const errorCode = isBackendError ? 'QUEUE_BACKEND_ERROR' : 'QUEUE_TIMEOUT'
          const errorType = isBackendError ? 'queue_backend_error' : 'queue_timeout'
          const errorMessage = isBackendError
            ? 'Queue service temporarily unavailable, please retry later'
            : 'User message queue wait timeout, please retry later'
          const statusCode = isBackendError ? 500 : 503

          // 结构化性能日志，用于后续统计
          logger.performance('user_message_queue_error', {
            errorType,
            errorCode,
            accountId,
            statusCode,
            backendError: isBackendError ? queueResult.errorMessage : undefined
          })

          logger.warn(
            `📬 User message queue ${errorType} for MiniMax account ${accountId}`,
            isBackendError ? { backendError: queueResult.errorMessage } : {}
          )
          return {
            statusCode,
            headers: {
              'Content-Type': 'application/json',
              'x-user-message-queue-error': errorType
            },
            body: JSON.stringify({
              type: 'error',
              error: {
                type: errorType,
                code: errorCode,
                message: errorMessage
              }
            }),
            accountId
          }
        }
        if (queueResult.acquired && !queueResult.skipped) {
          queueLockAcquired = true
          queueRequestId = queueResult.requestId
          logger.debug(
            `📬 User message queue lock acquired for MiniMax account ${accountId}, requestId: ${queueRequestId}`
          )
        }
      }

      // 获取账户信息
      account = await minimaxAccountService.getAccount(accountId)
      if (!account) {
        throw new Error('MiniMax account not found')
      }

      logger.info(
        `📤 Processing MiniMax API request for key: ${apiKeyData.name || apiKeyData.id}, account: ${account.name} (${accountId})`
      )
      logger.debug(`🌐 Account API URL: ${account.apiUrl}`)
      logger.debug(`🔍 Account supportedModels: ${JSON.stringify(account.supportedModels)}`)
      logger.debug(`🔑 Account has apiKey: ${!!account.apiKey}`)
      logger.debug(`📝 Request model: ${requestBody.model}`)

      // 处理模型前缀解析和映射
      const { baseModel } = parseVendorPrefixedModel(requestBody.model)
      logger.debug(`🔄 Parsed base model: ${baseModel} from original: ${requestBody.model}`)

      let mappedModel = baseModel
      if (
        account.supportedModels &&
        typeof account.supportedModels === 'object' &&
        !Array.isArray(account.supportedModels)
      ) {
        const newModel = minimaxAccountService.getMappedModel(account.supportedModels, baseModel)
        if (newModel !== baseModel) {
          logger.info(`🔄 Mapping model from ${baseModel} to ${newModel}`)
          mappedModel = newModel
        }
      }

      // 创建修改后的请求体，使用去前缀后的模型名
      const modifiedRequestBody = {
        ...requestBody,
        model: mappedModel
      }

      // 创建代理agent
      const proxyAgent = minimaxAccountService._createProxyAgent(account.proxy)

      // 创建AbortController用于取消请求
      abortController = new AbortController()

      // 设置客户端断开监听器
      const handleClientDisconnect = () => {
        logger.info('🔌 Client disconnected, aborting MiniMax request')
        if (abortController && !abortController.signal.aborted) {
          abortController.abort()
        }
      }

      // 监听客户端断开事件
      if (clientRequest) {
        clientRequest.once('close', handleClientDisconnect)
      }
      if (clientResponse) {
        clientResponse.once('close', handleClientDisconnect)
      }

      // 构建完整的API URL
      const cleanUrl = account.apiUrl.replace(/\/$/, '') // 移除末尾斜杠
      let apiEndpoint

      if (options.customPath) {
        // 如果指定了自定义路径（如 count_tokens），使用它
        const baseUrl = cleanUrl.replace(/\/v1\/messages$/, '') // 移除已有的 /v1/messages
        apiEndpoint = `${baseUrl}${options.customPath}`
      } else {
        // 默认使用 messages 端点
        apiEndpoint = cleanUrl.endsWith('/v1/messages') ? cleanUrl : `${cleanUrl}/v1/messages`
      }

      logger.debug(`🎯 Final API endpoint: ${apiEndpoint}`)
      logger.debug(`[DEBUG] Options passed to relayRequest: ${JSON.stringify(options)}`)
      logger.debug(`[DEBUG] Client headers received: ${JSON.stringify(clientHeaders)}`)

      // 过滤客户端请求头
      const filteredHeaders = this._filterClientHeaders(clientHeaders)
      logger.debug(`[DEBUG] Filtered client headers: ${JSON.stringify(filteredHeaders)}`)

      // 决定使用的 User-Agent：优先使用账户自定义的，否则透传客户端的，最后才使用默认值
      const userAgent =
        account.userAgent ||
        clientHeaders?.['user-agent'] ||
        clientHeaders?.['User-Agent'] ||
        this.defaultUserAgent

      // 准备请求配置
      const requestConfig = {
        method: 'POST',
        url: apiEndpoint,
        data: modifiedRequestBody,
        headers: {
          'Content-Type': 'application/json',
          'anthropic-version': '2023-06-01',
          'User-Agent': userAgent,
          ...filteredHeaders
        },
        timeout: config.requestTimeout || 600000,
        signal: abortController.signal,
        validateStatus: () => true // 接受所有状态码
      }

      if (proxyAgent) {
        requestConfig.httpAgent = proxyAgent
        requestConfig.httpsAgent = proxyAgent
        requestConfig.proxy = false
      }

      // MiniMax 统一使用 x-api-key 认证
      requestConfig.headers['x-api-key'] = account.apiKey
      logger.debug('[DEBUG] Using x-api-key authentication for MiniMax')

      logger.debug(
        `[DEBUG] Initial headers before beta: ${JSON.stringify(requestConfig.headers, null, 2)}`
      )

      // 添加beta header如果需要
      if (options.betaHeader) {
        logger.debug(`[DEBUG] Adding beta header: ${options.betaHeader}`)
        requestConfig.headers['anthropic-beta'] = options.betaHeader
      } else {
        logger.debug('[DEBUG] No beta header to add')
      }

      // 发送请求
      logger.debug(
        '📤 Sending request to MiniMax API with headers:',
        JSON.stringify(requestConfig.headers, null, 2)
      )
      const response = await axios(requestConfig)

      // 📬 请求已发送成功，立即释放队列锁（无需等待响应处理完成）
      if (queueLockAcquired && queueRequestId && accountId) {
        try {
          await userMessageQueueService.releaseQueueLock(accountId, queueRequestId)
          queueLockAcquired = false // 标记已释放，防止 finally 重复释放
          logger.debug(
            `📬 User message queue lock released early for MiniMax account ${accountId}, requestId: ${queueRequestId}`
          )
        } catch (releaseError) {
          logger.error(
            `❌ Failed to release user message queue lock early for MiniMax account ${accountId}:`,
            releaseError.message
          )
        }
      }

      // 移除监听器（请求成功完成）
      if (clientRequest) {
        clientRequest.removeListener('close', handleClientDisconnect)
      }
      if (clientResponse) {
        clientResponse.removeListener('close', handleClientDisconnect)
      }

      logger.debug(`🔗 MiniMax API response: ${response.status}`)
      logger.debug(`[DEBUG] Response headers: ${JSON.stringify(response.headers)}`)
      logger.debug(`[DEBUG] Response data type: ${typeof response.data}`)
      logger.debug(
        `[DEBUG] Response data length: ${response.data ? (typeof response.data === 'string' ? response.data.length : JSON.stringify(response.data).length) : 0}`
      )
      logger.debug(
        `[DEBUG] Response data preview: ${typeof response.data === 'string' ? response.data.substring(0, 200) : JSON.stringify(response.data).substring(0, 200)}`
      )

      // 检查错误状态并相应处理
      if (response.status === 401) {
        logger.warn(`🚫 Unauthorized error detected for MiniMax account ${accountId}`)
        const autoProtectionDisabled =
          account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'
        if (!autoProtectionDisabled) {
          await upstreamErrorHelper.markTempUnavailable(accountId, 'minimax', 401).catch(() => {})
        }
      } else if (response.status === 429) {
        logger.warn(`🚫 Rate limit detected for MiniMax account ${accountId}`)
        // 收到429先检查是否因为超过了手动配置的每日额度
        await minimaxAccountService.checkQuotaUsage(accountId).catch((err) => {
          logger.error('❌ Failed to check quota after 429 error:', err)
        })

        await minimaxAccountService.markAccountRateLimited(accountId)
        const autoProtectionDisabled =
          account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'
        if (!autoProtectionDisabled) {
          await upstreamErrorHelper
            .markTempUnavailable(
              accountId,
              'minimax',
              429,
              upstreamErrorHelper.parseRetryAfter(response.headers)
            )
            .catch(() => {})
        }
      } else if (response.status === 529) {
        logger.warn(`🚫 Overload error detected for MiniMax account ${accountId}`)
        await minimaxAccountService.markAccountOverloaded(accountId)
        const autoProtectionDisabled =
          account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'
        if (!autoProtectionDisabled) {
          await upstreamErrorHelper.markTempUnavailable(accountId, 'minimax', 529).catch(() => {})
        }
      } else if (response.status >= 500) {
        logger.warn(
          `🔥 Server error (${response.status}) detected for MiniMax account ${accountId}`
        )
        const autoProtectionDisabled =
          account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'
        if (!autoProtectionDisabled) {
          await upstreamErrorHelper
            .markTempUnavailable(accountId, 'minimax', response.status)
            .catch(() => {})
        }
      } else if (response.status === 200 || response.status === 201) {
        // 如果请求成功，检查并移除错误状态
        const isRateLimited = await minimaxAccountService.isAccountRateLimited(accountId)
        if (isRateLimited) {
          await minimaxAccountService.removeAccountRateLimit(accountId)
        }
        const isOverloaded = await minimaxAccountService.isAccountOverloaded(accountId)
        if (isOverloaded) {
          await minimaxAccountService.removeAccountOverload(accountId)
        }
      }

      // 更新最后使用时间
      await this._updateLastUsedTime(accountId)

      const responseBody =
        typeof response.data === 'string' ? response.data : JSON.stringify(response.data)
      logger.debug(`[DEBUG] Final response body to return: ${responseBody}`)

      return {
        statusCode: response.status,
        headers: response.headers,
        body: responseBody,
        accountId
      }
    } catch (error) {
      // 处理特定错误
      if (error.name === 'AbortError' || error.code === 'ECONNABORTED') {
        logger.info('Request aborted due to client disconnect')
        throw new Error('Client disconnected')
      }

      logger.error(
        `❌ MiniMax relay request failed (Account: ${account?.name || accountId}):`,
        error.message
      )

      // 网络错误标记临时不可用
      if (accountId && !error.response) {
        const autoProtectionDisabled =
          account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'
        if (!autoProtectionDisabled) {
          await upstreamErrorHelper.markTempUnavailable(accountId, 'minimax', 503).catch(() => {})
        }
      }

      throw error
    } finally {
      // 📬 释放用户消息队列锁（兜底，正常情况下已在请求发送后提前释放）
      if (queueLockAcquired && queueRequestId && accountId) {
        try {
          await userMessageQueueService.releaseQueueLock(accountId, queueRequestId)
          logger.debug(
            `📬 User message queue lock released in finally for MiniMax account ${accountId}, requestId: ${queueRequestId}`
          )
        } catch (releaseError) {
          logger.error(
            `❌ Failed to release user message queue lock for MiniMax account ${accountId}:`,
            releaseError.message
          )
        }
      }
    }
  }

  // 🌊 处理流式响应
  async relayStreamRequestWithUsageCapture(
    requestBody,
    apiKeyData,
    responseStream,
    clientHeaders,
    usageCallback,
    accountId,
    streamTransformer = null,
    options = {}
  ) {
    let account = null
    let queueLockAcquired = false
    let queueRequestId = null

    try {
      // 📬 用户消息队列处理
      if (userMessageQueueService.isUserMessageRequest(requestBody)) {
        // 校验 accountId 非空，避免空值污染队列锁键
        if (!accountId || accountId === '') {
          logger.error(
            '❌ accountId missing for queue lock in MiniMax relayStreamRequestWithUsageCapture'
          )
          throw new Error('accountId missing for queue lock')
        }
        const queueResult = await userMessageQueueService.acquireQueueLock(accountId)
        if (!queueResult.acquired && !queueResult.skipped) {
          // 区分 Redis 后端错误和队列超时
          const isBackendError = queueResult.error === 'queue_backend_error'
          const errorCode = isBackendError ? 'QUEUE_BACKEND_ERROR' : 'QUEUE_TIMEOUT'
          const errorType = isBackendError ? 'queue_backend_error' : 'queue_timeout'
          const errorMessage = isBackendError
            ? 'Queue service temporarily unavailable, please retry later'
            : 'User message queue wait timeout, please retry later'
          const statusCode = isBackendError ? 500 : 503

          // 结构化性能日志，用于后续统计
          logger.performance('user_message_queue_error', {
            errorType,
            errorCode,
            accountId,
            statusCode,
            stream: true,
            backendError: isBackendError ? queueResult.errorMessage : undefined
          })

          logger.warn(
            `📬 User message queue ${errorType} for MiniMax account ${accountId} (stream)`,
            isBackendError ? { backendError: queueResult.errorMessage } : {}
          )
          if (!responseStream.headersSent) {
            const existingConnection = responseStream.getHeader
              ? responseStream.getHeader('Connection')
              : null
            responseStream.writeHead(statusCode, {
              'Content-Type': 'text/event-stream',
              'Cache-Control': 'no-cache',
              Connection: existingConnection || 'keep-alive',
              'x-user-message-queue-error': errorType
            })
          }
          const errorEvent = `event: error\ndata: ${JSON.stringify({
            type: 'error',
            error: {
              type: errorType,
              code: errorCode,
              message: errorMessage
            }
          })}\n\n`
          responseStream.write(errorEvent)
          responseStream.write('data: [DONE]\n\n')
          responseStream.end()
          return
        }
        if (queueResult.acquired && !queueResult.skipped) {
          queueLockAcquired = true
          queueRequestId = queueResult.requestId
          logger.debug(
            `📬 User message queue lock acquired for MiniMax account ${accountId} (stream), requestId: ${queueRequestId}`
          )
        }
      }

      // 获取账户信息
      account = await minimaxAccountService.getAccount(accountId)
      if (!account) {
        throw new Error('MiniMax account not found')
      }

      logger.info(
        `📡 Processing streaming MiniMax API request for key: ${apiKeyData.name || apiKeyData.id}, account: ${account.name} (${accountId})`
      )
      logger.debug(`🌐 Account API URL: ${account.apiUrl}`)

      // 处理模型前缀解析和映射
      const { baseModel } = parseVendorPrefixedModel(requestBody.model)
      logger.debug(`🔄 Parsed base model: ${baseModel} from original: ${requestBody.model}`)

      let mappedModel = baseModel
      if (
        account.supportedModels &&
        typeof account.supportedModels === 'object' &&
        !Array.isArray(account.supportedModels)
      ) {
        const newModel = minimaxAccountService.getMappedModel(account.supportedModels, baseModel)
        if (newModel !== baseModel) {
          logger.info(`🔄 [Stream] Mapping model from ${baseModel} to ${newModel}`)
          mappedModel = newModel
        }
      }

      // 创建修改后的请求体，使用去前缀后的模型名
      const modifiedRequestBody = {
        ...requestBody,
        model: mappedModel
      }

      // 创建代理agent
      const proxyAgent = minimaxAccountService._createProxyAgent(account.proxy)

      // 发送流式请求
      await this._makeMinimaxStreamRequest(
        modifiedRequestBody,
        account,
        proxyAgent,
        clientHeaders,
        responseStream,
        accountId,
        usageCallback,
        streamTransformer,
        options,
        // 📬 回调：在收到响应头时释放队列锁
        async () => {
          if (queueLockAcquired && queueRequestId && accountId) {
            try {
              await userMessageQueueService.releaseQueueLock(accountId, queueRequestId)
              queueLockAcquired = false // 标记已释放，防止 finally 重复释放
              logger.debug(
                `📬 User message queue lock released early for MiniMax stream account ${accountId}, requestId: ${queueRequestId}`
              )
            } catch (releaseError) {
              logger.error(
                `❌ Failed to release user message queue lock early for MiniMax stream account ${accountId}:`,
                releaseError.message
              )
            }
          }
        }
      )

      // 更新最后使用时间
      await this._updateLastUsedTime(accountId)
    } catch (error) {
      // 客户端主动断开连接是正常情况，使用 INFO 级别
      if (error.message === 'Client disconnected') {
        logger.info(
          `🔌 MiniMax stream relay ended: Client disconnected (Account: ${account?.name || accountId})`
        )
      } else {
        logger.error(
          `❌ MiniMax stream relay failed (Account: ${account?.name || accountId}):`,
          error
        )
        // 网络错误标记临时不可用
        if (accountId && !error.response) {
          const autoProtectionDisabled =
            account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'
          if (!autoProtectionDisabled) {
            await upstreamErrorHelper.markTempUnavailable(accountId, 'minimax', 503).catch(() => {})
          }
        }
      }
      throw error
    } finally {
      // 📬 释放用户消息队列锁（兜底，正常情况下已在收到响应头后提前释放）
      if (queueLockAcquired && queueRequestId && accountId) {
        try {
          await userMessageQueueService.releaseQueueLock(accountId, queueRequestId)
          logger.debug(
            `📬 User message queue lock released in finally for MiniMax stream account ${accountId}, requestId: ${queueRequestId}`
          )
        } catch (releaseError) {
          logger.error(
            `❌ Failed to release user message queue lock for MiniMax stream account ${accountId}:`,
            releaseError.message
          )
        }
      }
    }
  }

  // 🌊 发送流式请求到MiniMax API
  async _makeMinimaxStreamRequest(
    body,
    account,
    proxyAgent,
    clientHeaders,
    responseStream,
    accountId,
    usageCallback,
    streamTransformer = null,
    requestOptions = {},
    onResponseHeaderReceived = null
  ) {
    return new Promise((resolve, reject) => {
      let aborted = false

      // 构建完整的API URL
      const cleanUrl = account.apiUrl.replace(/\/$/, '') // 移除末尾斜杠
      const apiEndpoint = cleanUrl.endsWith('/v1/messages') ? cleanUrl : `${cleanUrl}/v1/messages`

      logger.debug(`🎯 Final API endpoint for stream: ${apiEndpoint}`)

      // 过滤客户端请求头
      const filteredHeaders = this._filterClientHeaders(clientHeaders)
      logger.debug(`[DEBUG] Filtered client headers: ${JSON.stringify(filteredHeaders)}`)

      // 决定使用的 User-Agent：优先使用账户自定义的，否则透传客户端的，最后才使用默认值
      const userAgent =
        account.userAgent ||
        clientHeaders?.['user-agent'] ||
        clientHeaders?.['User-Agent'] ||
        this.defaultUserAgent

      // 准备请求配置
      const requestConfig = {
        method: 'POST',
        url: apiEndpoint,
        data: body,
        headers: {
          'Content-Type': 'application/json',
          'anthropic-version': '2023-06-01',
          'User-Agent': userAgent,
          ...filteredHeaders
        },
        timeout: config.requestTimeout || 600000,
        responseType: 'stream',
        validateStatus: () => true // 接受所有状态码
      }

      if (proxyAgent) {
        requestConfig.httpAgent = proxyAgent
        requestConfig.httpsAgent = proxyAgent
        requestConfig.proxy = false
      }

      // MiniMax 统一使用 x-api-key 认证
      requestConfig.headers['x-api-key'] = account.apiKey
      logger.debug('[DEBUG] Using x-api-key authentication for MiniMax')

      // 添加beta header如果需要
      if (requestOptions.betaHeader) {
        requestConfig.headers['anthropic-beta'] = requestOptions.betaHeader
      }

      // 发送请求
      const request = axios(requestConfig)

      request
        .then(async (response) => {
          logger.debug(`🌊 MiniMax stream response status: ${response.status}`)

          // 错误响应处理
          if (response.status !== 200) {
            logger.error(
              `❌ MiniMax API returned error status: ${response.status} | Account: ${account?.name || accountId}`
            )

            const autoProtectionDisabled =
              account?.disableAutoProtection === true || account?.disableAutoProtection === 'true'

            if (response.status === 401) {
              if (!autoProtectionDisabled) {
                upstreamErrorHelper.markTempUnavailable(accountId, 'minimax', 401).catch(() => {})
              }
            } else if (response.status === 429) {
              minimaxAccountService.markAccountRateLimited(accountId)
              if (!autoProtectionDisabled) {
                upstreamErrorHelper
                  .markTempUnavailable(
                    accountId,
                    'minimax',
                    429,
                    upstreamErrorHelper.parseRetryAfter(response.headers)
                  )
                  .catch(() => {})
              }
              // 检查是否因为超过每日额度
              minimaxAccountService.checkQuotaUsage(accountId).catch((err) => {
                logger.error('❌ Failed to check quota after 429 error:', err)
              })
            } else if (response.status === 529) {
              minimaxAccountService.markAccountOverloaded(accountId)
              if (!autoProtectionDisabled) {
                upstreamErrorHelper.markTempUnavailable(accountId, 'minimax', 529).catch(() => {})
              }
            } else if (response.status >= 500) {
              if (!autoProtectionDisabled) {
                upstreamErrorHelper
                  .markTempUnavailable(accountId, 'minimax', response.status)
                  .catch(() => {})
              }
            }

            // 设置错误响应的状态码和响应头
            if (!responseStream.headersSent) {
              const existingConnection = responseStream.getHeader
                ? responseStream.getHeader('Connection')
                : null
              const errorHeaders = {
                'Content-Type': response.headers['content-type'] || 'application/json',
                'Cache-Control': 'no-cache',
                Connection: existingConnection || 'keep-alive'
              }
              // 避免 Transfer-Encoding 冲突，让 Express 自动处理
              delete errorHeaders['Transfer-Encoding']
              delete errorHeaders['Content-Length']
              responseStream.writeHead(response.status, errorHeaders)
            }

            // 直接透传错误数据，不进行包装
            response.data.on('data', (chunk) => {
              if (isStreamWritable(responseStream)) {
                responseStream.write(chunk)
              }
            })

            response.data.on('end', () => {
              if (isStreamWritable(responseStream)) {
                responseStream.end()
              }
              resolve() // 不抛出异常，正常完成流处理
            })
            return
          }

          // 📬 收到成功响应头（HTTP 200），调用回调释放队列锁
          if (onResponseHeaderReceived && typeof onResponseHeaderReceived === 'function') {
            try {
              await onResponseHeaderReceived()
            } catch (callbackError) {
              logger.error(
                `❌ Failed to execute onResponseHeaderReceived callback for MiniMax stream account ${accountId}:`,
                callbackError.message
              )
            }
          }

          // 成功响应，检查并移除错误状态
          minimaxAccountService.isAccountRateLimited(accountId).then((isRateLimited) => {
            if (isRateLimited) {
              minimaxAccountService.removeAccountRateLimit(accountId)
            }
          })
          minimaxAccountService.isAccountOverloaded(accountId).then((isOverloaded) => {
            if (isOverloaded) {
              minimaxAccountService.removeAccountOverload(accountId)
            }
          })

          // 设置响应头
          if (!responseStream.headersSent) {
            const existingConnection = responseStream.getHeader
              ? responseStream.getHeader('Connection')
              : null
            if (existingConnection) {
              logger.debug(
                `🔌 [MiniMax Stream] Preserving existing Connection header: ${existingConnection}`
              )
            }
            const headers = {
              'Content-Type': 'text/event-stream',
              'Cache-Control': 'no-cache',
              Connection: existingConnection || 'keep-alive',
              'Access-Control-Allow-Origin': '*',
              'Access-Control-Allow-Headers': 'Cache-Control'
            }
            responseStream.writeHead(200, headers)
          }

          // 处理流数据和使用统计收集
          let rawBuffer = ''
          const collectedUsage = {}

          response.data.on('data', (chunk) => {
            if (aborted || responseStream.destroyed) {
              return
            }

            try {
              const chunkStr = chunk.toString('utf8')
              rawBuffer += chunkStr

              // 按行分割处理 SSE 数据
              const lines = rawBuffer.split('\n')
              rawBuffer = lines.pop() // 保留最后一个可能不完整的行

              for (const line of lines) {
                if (line.trim()) {
                  // 解析 SSE 数据并收集使用统计
                  const usageData = this._parseSSELineForUsage(line)
                  if (usageData) {
                    Object.assign(collectedUsage, usageData)
                  }

                  // 应用流转换器（如果提供）
                  let outputLine = line
                  if (streamTransformer && typeof streamTransformer === 'function') {
                    outputLine = streamTransformer(line)
                  }

                  // 写入到响应流
                  if (outputLine && isStreamWritable(responseStream)) {
                    responseStream.write(`${outputLine}\n`)
                  } else if (outputLine) {
                    // 客户端连接已断开，记录警告
                    logger.warn(
                      `⚠️ [MiniMax] Client disconnected during stream, skipping data for account: ${accountId}`
                    )
                  }
                } else {
                  // 空行也需要传递
                  if (isStreamWritable(responseStream)) {
                    responseStream.write('\n')
                  }
                }
              }
            } catch (err) {
              logger.error('❌ Error processing SSE chunk:', err)
            }
          })

          response.data.on('end', () => {
            // 如果收集到使用统计数据，调用回调
            if (usageCallback && Object.keys(collectedUsage).length > 0) {
              try {
                logger.debug(`📊 Collected usage data: ${JSON.stringify(collectedUsage)}`)
                // 在 usage 回调中包含模型信息
                usageCallback({ ...collectedUsage, accountId, model: body.model })
              } catch (err) {
                logger.error('❌ Error in usage callback:', err)
              }
            }

            if (isStreamWritable(responseStream)) {
              // 等待数据完全 flush 到客户端后再 resolve
              responseStream.end(() => {
                logger.debug(
                  `🌊 MiniMax stream response completed and flushed | bytesWritten: ${responseStream.bytesWritten || 'unknown'}`
                )
                resolve()
              })
            } else {
              // 连接已断开，记录警告
              logger.warn(
                `⚠️ [MiniMax] Client disconnected before stream end, data may not have been received | account: ${accountId}`
              )
              resolve()
            }
          })

          response.data.on('error', (err) => {
            logger.error('❌ Stream data error:', err)
            if (isStreamWritable(responseStream)) {
              responseStream.end()
            }
            reject(err)
          })

          // 客户端断开处理
          responseStream.on('close', () => {
            logger.info('🔌 Client disconnected from MiniMax stream')
            aborted = true
            if (response.data && typeof response.data.destroy === 'function') {
              response.data.destroy()
            }
          })

          responseStream.on('error', (err) => {
            logger.error('❌ Response stream error:', err)
            aborted = true
          })
        })
        .catch((error) => {
          if (!responseStream.headersSent) {
            responseStream.writeHead(500, { 'Content-Type': 'application/json' })
          }

          const errorResponse = {
            error: {
              type: 'internal_error',
              message: 'MiniMax API request failed'
            }
          }

          if (isStreamWritable(responseStream)) {
            responseStream.write(`data: ${JSON.stringify(errorResponse)}\n\n`)
            responseStream.end()
          }

          reject(error)
        })
    })
  }

  // 📊 解析SSE行以提取使用统计信息
  _parseSSELineForUsage(line) {
    try {
      if (line.startsWith('data: ')) {
        const data = line.substring(6).trim()
        if (data === '[DONE]') {
          return null
        }

        const jsonData = JSON.parse(data)

        // 检查是否包含使用统计信息
        if (jsonData.usage) {
          return {
            input_tokens: jsonData.usage.input_tokens || 0,
            output_tokens: jsonData.usage.output_tokens || 0,
            cache_creation_input_tokens: jsonData.usage.cache_creation_input_tokens || 0,
            cache_read_input_tokens: jsonData.usage.cache_read_input_tokens || 0,
            // 支持 ephemeral cache 字段
            cache_creation_input_tokens_ephemeral_5m:
              jsonData.usage.cache_creation_input_tokens_ephemeral_5m || 0,
            cache_creation_input_tokens_ephemeral_1h:
              jsonData.usage.cache_creation_input_tokens_ephemeral_1h || 0
          }
        }

        // 检查 message_delta 事件中的使用统计
        if (jsonData.type === 'message_delta' && jsonData.delta && jsonData.delta.usage) {
          return {
            input_tokens: jsonData.delta.usage.input_tokens || 0,
            output_tokens: jsonData.delta.usage.output_tokens || 0,
            cache_creation_input_tokens: jsonData.delta.usage.cache_creation_input_tokens || 0,
            cache_read_input_tokens: jsonData.delta.usage.cache_read_input_tokens || 0,
            cache_creation_input_tokens_ephemeral_5m:
              jsonData.delta.usage.cache_creation_input_tokens_ephemeral_5m || 0,
            cache_creation_input_tokens_ephemeral_1h:
              jsonData.delta.usage.cache_creation_input_tokens_ephemeral_1h || 0
          }
        }
      }
    } catch (err) {
      // 忽略解析错误，不是所有行都包含 JSON
    }

    return null
  }

  // 🔍 过滤客户端请求头
  _filterClientHeaders(clientHeaders) {
    if (!clientHeaders) {
      return {}
    }

    const filteredHeaders = {}
    const allowedHeaders = [
      'accept-language',
      'anthropic-beta',
      'anthropic-dangerous-direct-browser-access'
    ]

    // 只保留允许的头部信息
    for (const [key, value] of Object.entries(clientHeaders)) {
      const lowerKey = key.toLowerCase()
      if (allowedHeaders.includes(lowerKey)) {
        filteredHeaders[key] = value
      }
    }

    return filteredHeaders
  }

  // ⏰ 更新账户最后使用时间
  async _updateLastUsedTime(accountId) {
    try {
      const redis = require('../../models/redis')
      const client = redis.getClientSafe()
      await client.hset(`minimax_account:${accountId}`, 'lastUsedAt', new Date().toISOString())
    } catch (error) {
      logger.error(`❌ Failed to update last used time for MiniMax account ${accountId}:`, error)
    }
  }
}

module.exports = new MinimaxRelayService()
