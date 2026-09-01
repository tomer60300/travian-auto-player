/**
 * Every page must survive being rendered. That is the whole test.
 *
 * Three white-screen crashes reached the operator in one week -- all three the
 * same shape, `Cannot access 'X' before initialization`, thrown while React
 * evaluated a hook's dependency array against a `const` declared further down
 * the component. Every gate passed each time: the bundle built, eslint was
 * clean, and all 109 unit tests went green, because every one of those tests
 * lives in `src/utils/` and not one of them ever rendered a component. The
 * pipeline had no step that ran the pages.
 *
 * This is that step. It renders each page with `react-dom/server`, which needs
 * no browser and no new dependency (react-dom is already here), and which
 * evaluates exactly the code path those crashes lived in: module init, the
 * component body, and every hook call up to the first commit. A temporal-dead-
 * zone reference throws there, so it fails here.
 *
 * What this deliberately does NOT claim to be: a browser test. There is no DOM,
 * no layout, no click, no effect -- `useEffect` never runs under
 * renderToString. It cannot see a broken fetch, a mis-wired handler, or
 * anything that only appears after mount. It catches the class of bug that
 * turns the page white before the operator can do anything at all, which is the
 * class that actually escaped.
 */

import React from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeAll, describe, expect, it, vi } from 'vitest'

// Vite resolves this at build time, so a new page under src/pages is picked up
// without anyone remembering to add it here. That matters more than it looks:
// the entire failure mode being guarded against is a page nobody thought to
// check.
const PAGES = import.meta.glob('./*.jsx')

const NAMES = Object.keys(PAGES)
  .map((path) => path.replace('./', '').replace('.jsx', ''))
  .sort()

beforeAll(() => {
  // renderToString runs in Node, so anything a module reads from the browser at
  // import time has to exist. Kept to the smallest possible surface: a store
  // reading a token at module load is the only real user today, and a bigger
  // shim would start hiding the very breakage this file exists to surface.
  if (typeof globalThis.localStorage === 'undefined') {
    const store = new Map()
    globalThis.localStorage = {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
    }
  }
  if (typeof globalThis.matchMedia === 'undefined') {
    globalThis.matchMedia = () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    })
  }
  // A page that logs on render would spam the run; failures still surface,
  // because they throw rather than log.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

describe('every page renders', () => {
  it('finds the pages to check', () => {
    // Guards the glob itself: if the pattern ever stops matching, the suite
    // below would pass by testing nothing at all.
    expect(NAMES.length).toBeGreaterThanOrEqual(16)
    expect(NAMES).toContain('ResourcePlanner')
  })

  it.each(NAMES)('%s', async (name) => {
    const mod = await PAGES[`./${name}.jsx`]()
    const Page = mod.default

    expect(Page, `${name}.jsx has no default export`).toBeTypeOf('function')

    let html
    try {
      html = renderToString(
        React.createElement(MemoryRouter, null, React.createElement(Page))
      )
    } catch (err) {
      // The message this exists for reads "Cannot access 'un' before
      // initialization" in a production bundle -- unhelpful on its own, which
      // is why the page name and the real cause are spelled out here.
      throw new Error(
        `${name} threw while rendering, which is a white screen for the ` +
          `operator: ${err.message}\n` +
          `A "cannot access X before initialization" here means a hook's ` +
          `dependency array names something declared further down the ` +
          `component -- dependency arrays are evaluated during render, so the ` +
          `declaration has to come first.`
      )
    }

    expect(typeof html).toBe('string')
  })
})

describe('the guard is honest about what it does not do', () => {
  it('renders without running effects, and says so', () => {
    // Pinning the limitation as a fact rather than a comment: if someone later
    // sees this file pass and assumes the page is verified end to end, this is
    // the test that tells them otherwise.
    let effectRan = false
    function Probe() {
      React.useEffect(() => {
        effectRan = true
      }, [])
      return React.createElement('div', null, 'probe')
    }

    renderToString(React.createElement(Probe))

    expect(effectRan).toBe(false)
  })

  it('does catch a dependency array that reaches forward', () => {
    // The historical shape in miniature, so the guard's power is demonstrated
    // rather than asserted.
    //
    // Built with `new Function` for a reason worth recording: written plainly,
    // eslint REJECTS this component outright ("Cannot access variable before it
    // is declared"), which means the linter can catch this class statically and
    // the real crashes escaped only because an eslint-disable comment copied
    // onto the hook had silenced the rule there. So the two guards cover
    // different holes -- eslint catches the shape unless someone disables it,
    // and this catches it whatever the comments say. The fixture is constructed
    // at runtime so that stating that fact does not itself fail the lint.
    const makeBroken = new Function(
      'React',
      'return function Broken() {' +
        '  const cb = React.useCallback(() => later, [later]);' +
        '  const later = 1;' +
        '  return React.createElement("div", null, String(cb && later));' +
        '}'
    )
    const Broken = makeBroken(React)

    expect(() => renderToString(React.createElement(Broken))).toThrow(
      /before initialization/
    )
  })
})
