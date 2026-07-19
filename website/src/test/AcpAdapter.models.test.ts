import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the API client so fetchAvailableModels reads our canned /api/models.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

describe('AcpAdapter.fetchAvailableModels', () => {
  beforeEach(() => vi.clearAllMocks())

  it('returns backend-advertised models on success', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'auto', description: 'Let the provider pick' },
      { model_name: 'claude-opus-4.8', description: 'Most capable' },
      { model_name: 'claude-sonnet-4.6', description: 'Everyday tasks' },
    ])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.length).toBe(3)
    expect(models[0].name).toBe('auto')
    expect(models[1].name).toBe('claude-opus-4.8')
    expect(models[2].description).toBe('Everyday tasks')
  })

  it('falls back to static registry when API returns non-array (e.g. error object)', async () => {
    ;(api.models as any).mockResolvedValue({ error: 'Token required' })
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.length).toBeGreaterThan(0)
    expect(models.some(m => m.name.includes('opus') || m.name.includes('sonnet'))).toBe(true)
  })

  it('falls back to static registry when API returns empty array', async () => {
    ;(api.models as any).mockResolvedValue([])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.length).toBeGreaterThan(0)
  })

  it('falls back to static registry when API throws (timeout, network error)', async () => {
    ;(api.models as any).mockRejectedValue(new Error('fetch timeout'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.length).toBeGreaterThan(0)
    expect(models.some(m => m.name.includes('opus'))).toBe(true)
  })

  it('static fallback includes context window from registry', async () => {
    ;(api.models as any).mockRejectedValue(new Error('boom'))
    const models = await new AcpAdapter().fetchAvailableModels()
    const opus1m = models.find(m => m.name === 'opus-4.8-1m')
    expect(opus1m).toBeDefined()
    expect(opus1m!.contextWindow).toBe(1_000_000)
  })
})
