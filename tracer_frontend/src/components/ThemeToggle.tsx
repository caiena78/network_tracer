import React from 'react';
import { Sun, Moon } from 'lucide-react';
import { useThemeStore } from '../store/themeStore';

export default function ThemeToggle() {
  const { mode, toggleTheme } = useThemeStore();

  return (
    <button
      onClick={toggleTheme}
      className="btn btn-ghost"
      title={mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        padding: '6px 10px',
        fontSize: '13px',
        color: 'var(--text-secondary)',
      }}
    >
      {mode === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
      <span style={{ fontSize: '12px' }}>{mode === 'dark' ? 'Light' : 'Dark'}</span>
    </button>
  );
}
