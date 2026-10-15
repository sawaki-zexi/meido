import { useEffect, useRef, useState } from "react";
import { errorMessage } from "../api/client";
import { Icon } from "./Icon";

type ApiKeyInputProps = {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  configured: boolean;
  disabled?: boolean;
  placeholder?: string;
  onReveal: () => Promise<string>;
};

export function ApiKeyInput({ id, label, value, onChange, configured, disabled, placeholder, onReveal }: ApiKeyInputProps) {
  const [visible, setVisible] = useState(false);
  const [savedKey, setSavedKey] = useState("");
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const revealRequest = useRef(0);

  useEffect(() => {
    revealRequest.current += 1;
    setVisible(false);
    setSavedKey("");
    setReading(false);
    setError("");
    return () => { revealRequest.current += 1; };
  }, [id, configured, disabled]);
  useEffect(() => {
    revealRequest.current += 1;
    setSavedKey("");
    setReading(false);
    setError("");
    if (!value) setVisible(false);
  }, [value]);
  const showing = visible && !disabled;

  const toggleVisibility = async () => {
    if (disabled || reading) return;
    if (visible) {
      setVisible(false);
      setSavedKey("");
    } else if (value) {
      setVisible(true);
    } else {
      const request = ++revealRequest.current;
      setError("");
      setReading(true);
      try {
        const key = await onReveal();
        if (request !== revealRequest.current) return;
        setSavedKey(key);
        setVisible(true);
      } catch (cause) {
        if (request === revealRequest.current) setError(errorMessage(cause, "读取 API Key 失败"));
      } finally {
        if (request === revealRequest.current) setReading(false);
      }
    }
  };

  const changeDraft = (next: string) => {
    revealRequest.current += 1;
    setReading(false);
    setSavedKey("");
    setError("");
    if (!next) setVisible(false);
    onChange(next);
  };

  return <div className="field api-key-field">
    <label htmlFor={id}>{label}</label>
    <div className="api-key-input-row">
      <input id={id} className={configured && !value ? "api-key-saved" : undefined} type={showing ? "text" : "password"} autoComplete="new-password" value={value || (showing ? savedKey : "")} onChange={(event) => changeDraft(event.target.value)} disabled={disabled} placeholder={configured && !value ? "••••••" : placeholder} aria-describedby={error ? `${id}-error` : undefined} aria-invalid={error ? true : undefined} />
      <button type="button" className="api-key-visibility" aria-label={reading ? `读取${label}…` : `${showing ? "隐藏" : "显示"}${label}`} aria-pressed={showing} disabled={disabled || reading || (!configured && !value)} onClick={() => void toggleVisibility()}>{reading ? <span className="spinner" aria-hidden="true" /> : <Icon name={showing ? "eyeOff" : "eye"} />}</button>
    </div>
    {reading && <small className="field-hint" role="status">正在读取{label}…</small>}
    {error && <small id={`${id}-error`} className="api-key-error" role="alert">{error}</small>}
  </div>;
}
