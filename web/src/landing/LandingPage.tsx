import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { endingLines, openingLines, topics, type TopicId } from "./content";

type Phase = "title" | "transition" | "dialogue" | "choices" | "finished";
type Segment = "opening" | "topic" | "ending";
type Speed = "slow" | "normal" | "fast";
type LogEntry = { kind: "line" | "choice"; text: string };

const speedMs: Record<Speed, number> = { slow: 52, normal: 30, fast: 14 };
const spriteBySegment: Record<Segment, string> = {
  opening: "greeting",
  topic: "attentive",
  ending: "smile",
};
const githubUrl = "https://github.com/sawaki-zexi/meido";
const speakerName = "女仆";

function getTopic(id: TopicId) {
  return topics.find((topic) => topic.id === id)!;
}

export function LandingPage() {
  const [phase, setPhase] = useState<Phase>("title");
  const [segment, setSegment] = useState<Segment>("opening");
  const [lines, setLines] = useState(openingLines);
  const [lineIndex, setLineIndex] = useState(0);
  const [visibleCharacters, setVisibleCharacters] = useState(0);
  const [currentTopic, setCurrentTopic] = useState<TopicId | null>(null);
  const [visited, setVisited] = useState<TopicId[]>([]);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [autoPlay, setAutoPlay] = useState(false);
  const [speed, setSpeed] = useState<Speed>("normal");
  const [motionDisabled, setMotionDisabled] = useState(false);
  const [systemReducedMotion, setSystemReducedMotion] = useState(false);
  const [modal, setModal] = useState<"backlog" | "settings" | null>(null);
  const [focusedChoice, setFocusedChoice] = useState(0);
  const transitionTimer = useRef<number | undefined>(undefined);
  const choiceListRef = useRef<HTMLDivElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);

  const reducedMotion = motionDisabled || systemReducedMotion;
  const currentText = lines[lineIndex] ?? "";
  const characters = useMemo(() => Array.from(currentText), [currentText]);
  const displayedText = characters.slice(0, visibleCharacters).join("");

  const enterDialogue = useCallback((nextSegment: Segment, nextLines: string[]) => {
    setSegment(nextSegment);
    setLines(nextLines);
    setLineIndex(0);
    setVisibleCharacters(0);
    setLog((entries) => [...entries, { kind: "line", text: nextLines[0] ?? "" }]);
    setPhase("dialogue");
  }, []);

  const begin = useCallback(() => {
    if (phase !== "title") return;
    if (reducedMotion) {
      enterDialogue("opening", openingLines);
      return;
    }
    setPhase("transition");
    transitionTimer.current = window.setTimeout(() => enterDialogue("opening", openingLines), 760);
  }, [enterDialogue, phase, reducedMotion]);

  const advance = useCallback(() => {
    if (phase !== "dialogue") return;
    if (visibleCharacters < characters.length) {
      setVisibleCharacters(characters.length);
      return;
    }
    if (lineIndex + 1 < lines.length) {
      const nextIndex = lineIndex + 1;
      setLineIndex(nextIndex);
      setVisibleCharacters(0);
      setLog((entries) => [...entries, { kind: "line", text: lines[nextIndex] }]);
      return;
    }
    if (segment === "ending") {
      setPhase("finished");
      return;
    }
    setFocusedChoice(-1);
    setPhase("choices");
  }, [characters.length, lineIndex, lines, phase, segment, visibleCharacters]);

  const selectTopic = useCallback((topicId: TopicId) => {
    const topic = getTopic(topicId);
    setCurrentTopic(topicId);
    setVisited((current) => current.includes(topicId) ? current : [...current, topicId]);
    setLog((entries) => [...entries, { kind: "choice", text: topic.label }]);
    enterDialogue("topic", [...topic.lines, "主人还想了解哪一部分呢？"]);
  }, [enterDialogue]);

  const choose = useCallback((index: number) => {
    if (index === topics.length) {
      setCurrentTopic(null);
      setLog((entries) => [...entries, { kind: "choice", text: "没什么想问的了" }]);
      enterDialogue("ending", endingLines);
      return;
    }
    selectTopic(topics[index].id);
  }, [enterDialogue, selectTopic]);

  const skipSegment = useCallback(() => {
    if (phase !== "dialogue") return;
    setLog((entries) => {
      const additions = lines.slice(lineIndex + 1).map((text) => ({ kind: "line" as const, text }));
      return [...entries, ...additions];
    });
    setVisibleCharacters(characters.length);
    if (segment === "ending") setPhase("finished");
    else {
      setFocusedChoice(-1);
      setPhase("choices");
    }
  }, [characters.length, lineIndex, lines, phase, segment]);

  const restart = useCallback(() => {
    setCurrentTopic(null);
    setVisited([]);
    setLog([]);
    setModal(null);
    enterDialogue("opening", openingLines);
  }, [enterDialogue]);

  const returnToTitle = useCallback(() => {
    window.clearTimeout(transitionTimer.current);
    setModal(null);
    setPhase("title");
    setSegment("opening");
    setCurrentTopic(null);
    setLines(openingLines);
    setLineIndex(0);
    setVisibleCharacters(0);
    setVisited([]);
    setLog([]);
  }, []);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setSystemReducedMotion(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  useEffect(() => () => window.clearTimeout(transitionTimer.current), []);

  useEffect(() => {
    if (phase !== "dialogue" || visibleCharacters >= characters.length || reducedMotion) {
      if (phase === "dialogue" && reducedMotion) setVisibleCharacters(characters.length);
      return;
    }
    const timer = window.setTimeout(() => setVisibleCharacters((count) => Math.min(characters.length, count + 1)), speedMs[speed]);
    return () => window.clearTimeout(timer);
  }, [characters.length, phase, reducedMotion, speed, visibleCharacters]);

  useEffect(() => {
    if (!autoPlay || modal || phase !== "dialogue" || visibleCharacters < characters.length) return;
    const timer = window.setTimeout(advance, 1050);
    return () => window.clearTimeout(timer);
  }, [advance, autoPlay, characters.length, modal, phase, visibleCharacters]);

  useEffect(() => {
    if (modal) logEndRef.current?.scrollIntoView({ block: "end" });
  }, [log, modal]);

  useLayoutEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (phase === "title") {
        if (event.ctrlKey || event.altKey || event.metaKey || ["Shift", "Control", "Alt", "Meta"].includes(event.key)) return;
        event.preventDefault();
        begin();
        return;
      }
      if (event.key === "Escape") {
        if (modal) setModal(null);
        else if (phase === "choices" || phase === "finished") returnToTitle();
        return;
      }
      if (modal) return;
      const target = event.target as HTMLElement | null;
      const moveChoiceFocus = (direction: -1 | 1) => {
        const buttons = choiceListRef.current?.querySelectorAll<HTMLButtonElement>("button");
        if (!buttons?.length) return;
        event.preventDefault();
        const next = focusedChoice < 0
          ? direction > 0 ? 0 : buttons.length - 1
          : (focusedChoice + direction + buttons.length) % buttons.length;
        setFocusedChoice(next);
        buttons[next].focus();
      };
      if (target?.closest("button, a, input, select, textarea")) {
        if (phase === "choices" && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
          moveChoiceFocus(event.key === "ArrowDown" ? 1 : -1);
        }
        return;
      }
      if (phase === "dialogue" && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        advance();
      }
      if (phase === "choices" && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        moveChoiceFocus(event.key === "ArrowDown" ? 1 : -1);
      }
    };
    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [advance, begin, focusedChoice, modal, phase, returnToTitle]);

  const handleSceneClick = (event: React.MouseEvent<HTMLElement>) => {
    if (phase === "title") {
      begin();
      return;
    }
    if (phase !== "dialogue" || modal) return;
    const target = event.target as HTMLElement;
    if (!target.closest("button, a, input, select, textarea")) advance();
  };

  const sprite = phase === "title" || phase === "transition"
    ? "greeting"
    : spriteBySegment[segment];

  return <main
    className={`landing ${phase === "title" ? "landing-title" : "landing-scene"} ${reducedMotion ? "reduced-motion" : ""}`}
    onClick={handleSceneClick}
  >
    <img className="landing-background" src="/landing/night-sky.png" alt="" aria-hidden="true" />
    <div className="landing-vignette" aria-hidden="true" />
    <div className="landing-grain" aria-hidden="true" />

    {phase === "title" || phase === "transition" ? <section className="title-screen" aria-label="Meido 项目介绍标题画面">
      <div className="title-wordmark"><span className="title-kicker">A LOCAL-FIRST CHARACTER COMPANION</span><h1>meido</h1><span className="title-rule" /></div>
      <img className="title-sprite" src="/landing/elysia-greeting.png" alt="Meido 的女仆讲解角色" />
      <button className="start-prompt" onClick={(event) => { event.stopPropagation(); begin(); }} aria-label="按任意键开始游戏">
        <span>按任意键开始游戏</span>
      </button>
      {phase === "transition" && <div className="scene-transition" aria-hidden="true" />}
    </section> : <>
      <div className="scene-topbar">
        <div className="scene-brand"><span className="brand-mark">M</span><span>MEIDO</span><i />项目介绍</div>
      </div>

      <img className={`dialogue-sprite sprite-${sprite}`} src={`/landing/elysia-${sprite}.png`} alt="Meido 的女仆讲解角色" />

      <section className="dialogue-box" aria-live="polite">
        <span className="dialogue-corner corner-tl" aria-hidden="true">❧</span>
        <span className="dialogue-corner corner-tr" aria-hidden="true">❧</span>
        <span className="dialogue-corner corner-bl" aria-hidden="true">❧</span>
        <span className="dialogue-corner corner-br" aria-hidden="true">❧</span>
        <div className="speaker-name"><span className="speaker-emblem">✦</span>{speakerName}</div>
        <p className="dialogue-text">{displayedText}<span className={`text-caret ${visibleCharacters >= characters.length ? "caret-hidden" : ""}`} aria-hidden="true">▾</span></p>
        <div className="scene-controls" aria-label="对话控制">
          <button type="button" className={autoPlay ? "control-active" : ""} onClick={() => setAutoPlay((value) => !value)} title="自动播放" aria-label={autoPlay ? "关闭自动播放" : "开启自动播放"}><span className="control-icon" aria-hidden="true">▷</span>自动</button>
          <button type="button" onClick={skipSegment} disabled={phase !== "dialogue"} title="跳过当前段落" aria-label="跳过当前段落"><span className="control-icon" aria-hidden="true">»</span>跳过</button>
          <button type="button" onClick={() => setModal("backlog")} title="查看对话记录" aria-label="查看对话记录"><span className="control-icon" aria-hidden="true">↶</span>记录</button>
          <button type="button" onClick={() => setModal("settings")} title="打开设置" aria-label="打开设置"><span className="control-icon" aria-hidden="true">⚙</span>设置</button>
          <button type="button" onClick={returnToTitle} title="返回标题画面" aria-label="返回标题画面"><span className="control-icon" aria-hidden="true">⌂</span>标题</button>
        </div>
      </section>

      {phase === "choices" && <div className="choice-list" ref={choiceListRef} aria-label="项目模块选项">
        {topics.map((topic, index) => <button type="button" key={topic.id} className={`choice-button ${visited.includes(topic.id) ? "choice-visited" : ""}`} onFocus={() => setFocusedChoice(index)} onClick={() => choose(index)}>
          <span className="choice-index">{String(index + 1).padStart(2, "0")}</span><span>{topic.label}</span>
        </button>)}
        <button type="button" className="choice-button choice-exit" onFocus={() => setFocusedChoice(topics.length)} onClick={() => choose(topics.length)}>
          <span className="choice-index">06</span><span>没什么想问的了</span><span className="choice-arrow">→</span>
        </button>
      </div>}

      {phase === "finished" && <div className="ending-actions" aria-label="结束操作">
        <a className="ending-link ending-primary" href={githubUrl} target="_blank" rel="noreferrer">查看 Meido 项目 <span>↗</span></a>
        <button type="button" className="ending-link" onClick={restart}>重新开始</button>
        <button type="button" className="ending-link" onClick={returnToTitle}>返回标题</button>
      </div>}
    </>}

    {phase !== "title" && phase !== "transition" && <footer className="scene-footer"><span>MEIDO · PROJECT INTRODUCTION</span><span>{currentTopic ? getTopic(currentTopic).label : phase === "finished" ? "FIN" : "PROLOGUE"}</span></footer>}

    {modal && <div className="modal-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) setModal(null); }}>
      <section className={`utility-panel ${modal === "settings" ? "settings-panel" : "backlog-panel"}`} role="dialog" aria-modal="true" aria-labelledby="utility-title">
        <div className="utility-heading"><div><span>MEIDO · SYSTEM</span><h2 id="utility-title">{modal === "backlog" ? "对话记录" : "设置"}</h2></div><button type="button" className="close-control" onClick={() => setModal(null)} aria-label="关闭">×</button></div>
        {modal === "backlog" ? <div className="backlog-list">{log.map((entry, index) => <article className={`backlog-entry ${entry.kind}`} key={`${index}-${entry.text}`}>
          {entry.kind === "choice" ? <span className="backlog-choice">选择了　{entry.text}</span> : <><strong>{speakerName}</strong><p>{entry.text}</p></>}
        </article>)}<div ref={logEndRef} /></div> : <div className="settings-form">
          <fieldset><legend>文字速度</legend><div className="setting-options">
            {(["slow", "normal", "fast"] as const).map((value) => <button type="button" key={value} className={speed === value ? "setting-selected" : ""} onClick={() => setSpeed(value)}>{value === "slow" ? "慢" : value === "normal" ? "标准" : "快"}</button>)}
          </div></fieldset>
          <label className="setting-toggle"><span><strong>减少动态效果</strong><small>关闭逐字显示和转场动画</small></span><input type="checkbox" checked={motionDisabled} onChange={(event) => setMotionDisabled(event.target.checked)} /></label>
          {systemReducedMotion && <p className="system-motion-note">系统已启用减少动态效果。</p>}
        </div>}
      </section>
    </div>}
  </main>;
}
