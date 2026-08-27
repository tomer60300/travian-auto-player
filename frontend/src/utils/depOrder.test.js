import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { forwardRefsInDepArrays } from './depOrder'

const here = dirname(fileURLToPath(import.meta.url))

describe('forwardRefsInDepArrays', () => {
  it('catches the exact shape that crashed the planner twice', () => {
    // Prettier's actual output: the callback's closing `},` on its own line,
    // the opening `[` alone on the next. A fixture in any OTHER shape proves
    // nothing -- the first version of this very test used `() => {},` inline
    // and passed against a scanner that missed the production bug. Both
    // production crashes -- "Cannot access 'ln'/'un' before initialization" --
    // were this shape.
    const source = [
      'const runExecute = useCallback(',
      '  async (dryRun) => {',
      '    doTheThing()',
      '  },',
      '  [',
      '    accountKey,',
      '    buildExecutePayload,',
      '  ]',
      ')',
      'const buildExecutePayload = useCallback(() => {}, [])',
    ].join('\n')

    const offenders = forwardRefsInDepArrays(source)

    expect(offenders).toEqual([
      { line: 4, identifier: 'buildExecutePayload', declaredAt: 10 },
    ])
  })

  it('catches the single-line form too', () => {
    const source = ['const a = useCallback(() => {}, [b])', 'const b = 1'].join('\n')
    expect(forwardRefsInDepArrays(source)).toHaveLength(1)
  })

  it('accepts declarations that come first', () => {
    const source = [
      'const b = 1',
      'const a = useCallback(() => {}, [b])',
    ].join('\n')
    expect(forwardRefsInDepArrays(source)).toEqual([])
  })

  it('the Resource Planner itself has no forward references', () => {
    // The permanent guard. This is the file that crashed; any hook reordering
    // that reintroduces a use-before-declaration in a dependency array fails
    // here in vitest instead of at the operator's first render.
    const source = readFileSync(join(here, '..', 'pages', 'ResourcePlanner.jsx'), 'utf-8')

    expect(forwardRefsInDepArrays(source)).toEqual([])
  })
})
