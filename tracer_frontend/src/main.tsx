import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles/theme.css';
import './styles/globals.css';

// Apply the saved theme attribute immediately (index.html also does this
// inline but this ensures it is set even if the inline script was stripped).
(function ensureTheme() {
  try {
    const saved = localStorage.getItem('tracer-theme');
    if (saved === 'dark' || saved === 'light') {
      document.documentElement.setAttribute('data-theme', saved);
    } else if (!document.documentElement.getAttribute('data-theme')) {
      const preferred = window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light';
      document.documentElement.setAttribute('data-theme', preferred);
    }
  } catch {
    // ignore
  }
})();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
