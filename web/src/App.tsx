import { useEffect, useRef, useState } from "react";
import type { Role } from "./api/types";
import { ChatView, WelcomeView } from "./features/chat/ChatView";
import { useChat } from "./features/chat/useChat";
import { RoleEditor } from "./features/role-management/RoleEditor";
import { RoleList } from "./features/role-management/RoleList";
import { useRoles } from "./features/role-management/useRoles";
import { SettingsDialog } from "./features/settings/SettingsDialog";
import { CreateRolePage } from "./features/tavern/CreateRolePage";
import { RoleCard } from "./features/tavern/RoleCard";
import { TavernView } from "./features/tavern/TavernView";
import { Dialog } from "./ui/Dialog";
import { Icon, IconButton } from "./ui/Icon";
import { Notice } from "./ui/Status";

/** What the main pane shows: a conversation, or the role tavern (all role cards). */
type View = "chat" | "tavern";
/** Overlay on top of the main pane; null means none. "card" reveals a newly created role's card. */
type Panel = null | "create" | "profile" | "settings" | "card";

const LAST_ROLE_KEY = "meido:last-role";
const readLastRole = () => { try { return localStorage.getItem(LAST_ROLE_KEY); } catch { return null; } };
const writeLastRole = (id: string) => { try { localStorage.setItem(LAST_ROLE_KEY, id); } catch { /* storage unavailable */ } };

export function App() {
  const roles = useRoles();
  const chat = useChat();
  // A restored create-role draft reopens the create panel.
  const [panel, setPanel] = useState<Panel>(() => roles.creating ? "create" : null);
  const [view, setView] = useState<View>("chat");
  const [newRole, setNewRole] = useState<Role | null>(null);
  // Narrow screens show one pane at a time; home is the conversation.
  const [showList, setShowList] = useState(false);
  const [openingId, setOpeningId] = useState<string | null>(null);
  const booted = useRef(false);

  const openChat = async (role: Role) => {
    roles.setError("");
    setOpeningId(role.id);
    const opened = await chat.open(role);
    setOpeningId(null);
    if (!opened) return false;
    writeLastRole(role.id);
    setView("chat");
    setShowList(false);
    return true;
  };

  // Home is a conversation: once roles load, open the last role (or the first one).
  useEffect(() => {
    if (roles.loading || booted.current) return;
    booted.current = true;
    const home = roles.roles.find((role) => role.id === readLastRole()) ?? roles.roles[0];
    if (home) void openChat(home);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roles.loading]);

  const chatRole = roles.roles.find((role) => role.id === chat.roleId) ?? null;

  // The create page opens over whatever is showing, so closing it returns there.
  const startCreate = () => { roles.startCreate(); setPanel("create"); };
  const showProfile = (role: Role) => { roles.select(role); setPanel("profile"); };
  const openTavern = () => { setView("tavern"); setShowList(false); };
  const backToChat = () => { setView("chat"); setShowList(false); };
  const chatWith = (role: Role) => role.id === chat.roleId ? (setView("chat"), setShowList(false)) : void openChat(role);

  const closePanel = () => {
    // Closing the create panel keeps the draft (it is restored next time); closing a profile drops unsaved edits.
    if (panel === "profile" && roles.editing) roles.cancel();
    setPanel(null);
  };

  const saveRole = async () => {
    const wasCreating = roles.creating;
    const saved = await roles.save();
    if (!saved) return;
    // A new role gets her own card; the user starts chatting from there.
    if (wasCreating) { setNewRole(saved); setPanel("card"); }
  };

  const cancelEdit = () => {
    const wasCreating = roles.creating;
    roles.cancel();
    if (wasCreating) setPanel(null);
  };

  const deleteRole = async () => {
    const deletedId = roles.selected?.id;
    if (!await roles.remove()) return;
    setPanel(null);
    if (deletedId !== chat.roleId) return;
    chat.reset();
    // In the tavern the user stays with the cards; in a conversation, move on to the next role.
    const next = roles.roles.find((role) => role.id !== deletedId);
    if (next && view === "chat") void openChat(next);
  };

  // Switching roles mid-reply would mix two conversations, so the list waits for the reply.
  const lockSwitch = chat.sending || openingId !== null;

  return <div className={`shell${showList ? " show-list" : ""}`}>
    <aside className="sidebar" aria-label="对话列表">
      <header className="sidebar-header">
        <h1 className="brand">Meido</h1>
      </header>
      <nav className="side-tabs" aria-label="页面">
        <button type="button" aria-current={view === "chat" ? "page" : undefined} onClick={backToChat}><Icon name="chat" size={17} />对话</button>
        <button type="button" aria-current={view === "tavern" ? "page" : undefined} onClick={openTavern}><Icon name="cards" size={17} />角色</button>
      </nav>
      <div className="sidebar-body">
        {roles.error && panel === null && view === "chat" && <Notice onDismiss={() => roles.setError("")}>{roles.error}</Notice>}
        <RoleList roles={roles.roles} loading={roles.loading} loadFailed={roles.loadFailed} activeId={view === "chat" ? chat.roleId : null} disabled={lockSwitch} onOpen={chatWith} />
      </div>
      <footer className="sidebar-footer">
        <IconButton icon="settings" label="设置" onClick={() => setPanel("settings")} />
      </footer>
    </aside>

    <main className="main">
      {view === "tavern"
        ? <TavernView roles={roles.roles} loading={roles.loading} error={panel === null ? roles.error : ""} onDismissError={() => roles.setError("")} chatDisabled={lockSwitch} onBack={() => setShowList(true)} onCreate={startCreate} onShow={showProfile} onChat={chatWith} />
        : chatRole
        ? <ChatView role={chatRole} chat={chat} onBack={() => setShowList(true)} onShowProfile={() => showProfile(chatRole)} />
        : <WelcomeView loading={roles.loading || openingId !== null} hasRoles={roles.roles.length > 0} error={chat.error} onDismissError={() => chat.setError("")} onCreate={startCreate} onBack={() => setShowList(true)} />}
    </main>

    {panel === "create" && <Dialog title="创建角色" variant="page" onClose={closePanel}>
      <CreateRolePage state={roles} number={roles.roles.length + 1} onSubmit={() => void saveRole()} onCancel={cancelEdit} />
    </Dialog>}
    {panel === "profile" && <Dialog title="角色资料" variant="drawer" onClose={closePanel}>
      <RoleEditor state={roles} onSubmit={() => void saveRole()} onCancel={cancelEdit} onDelete={() => void deleteRole()} />
    </Dialog>}
    {panel === "settings" && <SettingsDialog onClose={() => setPanel(null)} />}
    {panel === "card" && newRole && <Dialog title="新卡牌" variant="compact" onClose={() => setPanel(null)}>
      <div className="card-reveal">
        <RoleCard role={newRole} number={roles.roles.findIndex((role) => role.id === newRole.id) + 1} className="is-new" />
        <p className="muted">{newRole.name} 加入了酒馆。</p>
        <div className="form-actions">
          <button type="button" className="ghost" onClick={() => setPanel(null)}>稍后再聊</button>
          <button type="button" className="primary" onClick={() => { setPanel(null); void openChat(newRole); }}>开始对话</button>
        </div>
      </div>
    </Dialog>}
  </div>;
}
