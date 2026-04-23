# React Frontend Conventions

- Functional components with hooks only. No class components.
- Zustand 5 for UI state only (modals, sidebar, form drafts). Server data via fetch/axios.
- Tailwind CSS v4: uses `@tailwindcss/vite` plugin — NO tailwind.config.ts, NO PostCSS config
- Component files: PascalCase (e.g., `UserCard.jsx`). Hooks: `useAuth.js`
- No `any` types. Named exports only. No console.log in production code.
- API calls: axios instance in `src/api.js`, all endpoints centralized there
- WebSocket: wrapper in `src/ws.js`, real-time log streaming via `src/logStream.js`
- Routing: react-router-dom v7, routes defined in `src/App.jsx`
- Build output: `../src/travian_api/web/static` (served by FastAPI)
- Dev server proxy: `/api` -> localhost:8000, `/ws` -> ws://localhost:8000
- Styles in `src/index.css` with Tailwind utility classes
- ESLint flat config in `eslint.config.js`
