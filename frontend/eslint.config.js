import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  // `dist` is the Vite output. The other two are Playwright's run artefacts,
  // and they have to be here rather than only in .gitignore: flat config does
  // not read .gitignore, so a single FAILING spec writes `e2e/report/trace/`
  // -- the bundled trace viewer, minified UMD -- and `npx eslint .` comes back
  // with 693 errors in code nobody wrote. A gate that breaks when a test fails
  // is a gate that stops being run.
  globalIgnores(['dist', 'e2e/report', 'test-results']),
  {
    // Config/tooling files run in Node, not the browser.
    files: ['*.config.{js,mjs}'],
    languageOptions: { globals: globals.node },
  },
  {
    files: ['**/*.{js,jsx}'],
    ignores: ['*.config.{js,mjs}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
      // React 19 strict rules — downgrade to warn for patterns common in this codebase
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/purity': 'off',  // too strict — flags ref reads during render, Date.now() in useMemo, etc.
      'react-hooks/set-state-in-effect': 'warn',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    },
  },
])
