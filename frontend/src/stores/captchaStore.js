import { create } from 'zustand'

const useCaptchaStore = create((set) => ({
  active: false,
  triggeredAt: null,
  pattern: null,
  url: null,
  statusCode: null,
  responseSnippet: null,

  trigger: (pattern, triggeredAt, { url, statusCode, responseSnippet } = {}) => set({
    active: true,
    triggeredAt: triggeredAt || Date.now(),
    pattern,
    url: url || null,
    statusCode: statusCode || null,
    responseSnippet: responseSnippet || null,
  }),

  resolve: () => set({
    active: false,
    triggeredAt: null,
    pattern: null,
    url: null,
    statusCode: null,
    responseSnippet: null,
  }),
}))

export default useCaptchaStore
