import { useEffect, useState } from 'react';

export function StudioProgress() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <div className="studio-progress" role="status"><span className="studio-progress-spinner" /><strong>正在绘制图片</strong><span>已等待 {seconds} 秒</span></div>;
}
