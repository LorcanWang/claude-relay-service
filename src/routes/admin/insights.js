const express = require('express')
const { authenticateAdmin } = require('../../middleware/auth')
const logger = require('../../utils/logger')
const operationalInsightsService = require('../../services/operationalInsightsService')

const router = express.Router()

function parseHoursParam(value, defaultValue = 24) {
  const parsed = parseInt(value || defaultValue, 10)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return defaultValue
  }

  return Math.min(parsed, 72)
}

router.get('/summary', authenticateAdmin, async (req, res) => {
  try {
    const data = await operationalInsightsService.getSummary()
    return res.json({ success: true, data })
  } catch (error) {
    logger.error('Failed to get operational insights summary:', error)
    return res
      .status(500)
      .json({ error: 'Failed to get operational insights summary', message: error.message })
  }
})

router.get('/hourly', authenticateAdmin, async (req, res) => {
  try {
    const hours = parseHoursParam(req.query.hours, 24)
    const data = await operationalInsightsService.getHourlyMetrics(hours)
    return res.json({ success: true, data, hours })
  } catch (error) {
    logger.error('Failed to get operational hourly metrics:', error)
    return res
      .status(500)
      .json({ error: 'Failed to get operational hourly metrics', message: error.message })
  }
})

router.get('/scheduler', authenticateAdmin, async (req, res) => {
  try {
    const hours = parseHoursParam(req.query.hours, 24)
    const data = await operationalInsightsService.getSchedulerStats(hours)
    return res.json({ success: true, data })
  } catch (error) {
    logger.error('Failed to get operational scheduler stats:', error)
    return res
      .status(500)
      .json({ error: 'Failed to get operational scheduler stats', message: error.message })
  }
})

router.get('/accounts', authenticateAdmin, async (req, res) => {
  try {
    const data = await operationalInsightsService.scanAccountPerformance()
    return res.json({ success: true, data, count: data.length })
  } catch (error) {
    logger.error('Failed to get operational account performance:', error)
    return res
      .status(500)
      .json({ error: 'Failed to get operational account performance', message: error.message })
  }
})

module.exports = router
