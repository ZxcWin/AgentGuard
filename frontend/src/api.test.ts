import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, money, number, sourceName, statusName } from './api'
afterEach(() => vi.unstubAllGlobals())
describe('API display contract', () => {
  it('retains estimates and unknown sources', () => {
    expect(money('0.012345')).toBe('$0.0123')
    expect(number(12345)).toBe('12,345')
    expect(sourceName('estimate')).toBe('估算')
    expect(sourceName('unknown')).toBe('未知')
    expect(statusName('failed')).toBe('失败')
  })
  it('surfaces policy conflicts with their status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: 'version changed' } }), { status: 409 })))
    await expect(api('/policies/current')).rejects.toMatchObject({ status: 409, message: 'version changed' })
  })
  it('never substitutes sample data when disconnected', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(api('/runs')).rejects.toThrow('Failed to fetch')
  })
  it('handles invalid error bodies', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('bad gateway', { status: 502 })))
    await expect(api('/health')).rejects.toBeInstanceOf(ApiError)
  })
})
