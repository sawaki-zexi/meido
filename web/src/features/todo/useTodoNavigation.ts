import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { TodoSettings } from '../../api/types';

/** One application-wide reminder badge, refreshed when configuration or the drawer closes. */
export function useTodoNavigation(overlayOpen: boolean) {
  const [enabled, setEnabled] = useState(false);
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    let active = true;
    let sequence = 0;
    const refresh = async () => {
      const request = ++sequence;
      const current = () => active && request === sequence;
      try {
        let settings = await api<TodoSettings>('/api/todos/settings');
        if (!current()) return;
        if (settings.timezoneConfigured === false) {
          const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
          if (timezone) settings = await api<TodoSettings>('/api/todos/settings', { method: 'PUT', body: JSON.stringify({ timezone }) });
        }
        if (!current()) return;
        setEnabled(settings.enabled);
        if (settings.enabled && settings.remindersEnabled) {
          const reminders = await api<{ unreadCount: number }>('/api/todos/reminders');
          if (current()) setUnread(reminders.unreadCount);
        } else setUnread(0);
      } catch { if (current()) setUnread(0); }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 20000);
    const onFocus = () => void refresh();
    window.addEventListener('focus', onFocus);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener('focus', onFocus); };
  }, [overlayOpen]);
  return { enabled, unread };
}
