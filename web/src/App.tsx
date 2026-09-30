import { useState } from "react";
import type { Role } from "./api/types";
import { ChatView } from "./features/chat/ChatView";
import { useChat } from "./features/chat/useChat";
import { ModelSettings } from "./features/model/ModelSettings";
import { RoleEditor } from "./features/role-management/RoleEditor";
import { RoleList } from "./features/role-management/RoleList";
import { useRoles } from "./features/role-management/useRoles";
import { Avatar } from "./ui/Avatar";
import { Notice, Placeholder } from "./ui/Status";

type View = "roles" | "chat" | "model";

export function App() {
  const roles = useRoles();
  const chat = useChat();
  const [view, setView] = useState<View>("roles");
  const [openingId, setOpeningId] = useState<string | null>(null);

  const openChat = async (role: Role) => {
    roles.setError("");
    setOpeningId(role.id);
    const opened = await chat.open(role);
    setOpeningId(null);
    if (!opened) return;
    // Keep an unsaved create-role draft; otherwise show this role when returning.
    if (!roles.creating) roles.select(role);
    setView("chat");
  };

  const deleteRole = async () => {
    const deletedId = roles.selected?.id;
    if (await roles.remove() && deletedId === chat.roleId) chat.reset();
  };

  const showRoles = () => { chat.setError(""); setView("roles"); };

  const chatRole = view === "chat" ? roles.roles.find((role) => role.id === chat.roleId) ?? null : null;
  const showDetail = roles.creating || roles.selected !== null;
  const lockNavigation = roles.busy;

  return <div className="app">
    <header className="topbar">
      {view === "roles"
        ? <h1 className="brand">Meido <span className="muted">· 角色</span></h1>
        : <button type="button" className="ghost back" disabled={lockNavigation} onClick={showRoles}>← 返回角色</button>}
      {chatRole && <div className="topbar-title"><Avatar id={chatRole.id} name={chatRole.name} size="sm" /><h1>{chatRole.name}</h1></div>}
      {view === "model" && <h1 className="topbar-title">模型设置</h1>}
      <nav className="topbar-actions" aria-label="主操作">
        {view !== "model" && <button type="button" className="ghost" disabled={lockNavigation} onClick={() => setView("model")}>模型设置</button>}
        {view === "roles" && <button type="button" className="primary" disabled={lockNavigation} onClick={roles.startCreate}>创建角色</button>}
      </nav>
    </header>

    {view === "model" ? <main className="page"><ModelSettings onBack={() => setView("roles")} /></main>
      : chatRole ? <main className="page page-chat"><ChatView role={chatRole} chat={chat} /></main>
      : <main className={`page roles-layout${showDetail ? " has-detail" : ""}`}>
        <aside className="roles-sidebar">
          {chat.error && view === "roles" && <Notice onDismiss={() => chat.setError("")}>{chat.error}</Notice>}
          {roles.error && !showDetail && <Notice onDismiss={() => roles.setError("")}>{roles.error}</Notice>}
          <RoleList roles={roles.roles} loading={roles.loading} loadFailed={roles.loadFailed} selectedId={roles.creating ? null : roles.selected?.id ?? null} onSelect={roles.select} onOpenChat={(role) => void openChat(role)} />
        </aside>
        <section className="roles-detail">
          {showDetail
            ? <RoleEditor state={roles} opening={openingId !== null} onOpenChat={() => roles.selected && void openChat(roles.selected)} onDelete={() => void deleteRole()} />
            : <Placeholder>选择角色或创建新角色。</Placeholder>}
        </section>
      </main>}
  </div>;
}
