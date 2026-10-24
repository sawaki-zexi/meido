import { useEffect, useRef, useState } from "react";
import type { Role } from "./api/types";
import { ChatView, WelcomeView } from "./features/chat/ChatView";
import { useChat } from "./features/chat/useChat";
import { MemoryPanel, type MemoryOrigin } from "./features/memory/MemoryPanel";
import { ChannelView, type ChannelMemorySource } from "./features/channels/ChannelView";
import { RoleList } from "./features/role-management/RoleList";
import { useRoles } from "./features/role-management/useRoles";
import { SettingsDialog, type SettingsSectionId } from "./features/settings/SettingsDialog";
import { applyAppearance, readAppearance } from './features/settings/appearance';
import { RoleAccountsPanel } from "./features/channels/RoleAccountsPanel";
import { CreateRolePage } from "./features/tavern/CreateRolePage";
import { RoleCard } from "./features/tavern/RoleCard";
import { TavernView } from "./features/tavern/TavernView";
import { Dialog } from "./ui/Dialog";
import { Icon, IconButton } from "./ui/Icon";
import { Notice } from "./ui/Status";
import { TodoDrawer } from './features/todo/TodoDrawer';
import { useTodoNavigation } from './features/todo/useTodoNavigation';
import { NovelaiStudio } from './features/novelai/NovelaiStudio';

/** What the main pane shows: a conversation, the role tavern, memories, channel threads, or the NovelAI studio. */
type View = "chat" | "tavern" | "memories" | "channels" | "novelai";
/** Overlay on top of the main pane; null means none. "card" reveals a newly created role's card. */
type Panel = null | "create" | "profile" | "settings" | "card";

const LAST_ROLE_KEY = "meido:last-role";
const readLastRole = () => { try { return localStorage.getItem(LAST_ROLE_KEY); } catch { return null; } };
const writeLastRole = (id: string) => { try { localStorage.setItem(LAST_ROLE_KEY, id); } catch { /* storage unavailable */ } };

export function App() {
  useEffect(() => { applyAppearance(readAppearance()); }, []);
  const roles = useRoles();
  const chat = useChat();
  const [returnedFromOwnerKnowledgeAuthorization] = useState(
    () => new URLSearchParams(window.location.search).get("ownerKnowledge") === "connected",
  );
  // A restored create-role draft reopens the create panel.
  const [panel, setPanel] = useState<Panel>(() => roles.creating ? "create" : returnedFromOwnerKnowledgeAuthorization ? "settings" : null);
  const [view, setView] = useState<View>("chat");
  const [studioRoleId, setStudioRoleId] = useState<string | null>(null);
  const [todoOpen, setTodoOpen] = useState(false);
  const todoNavigation = useTodoNavigation(panel === 'settings' || todoOpen);
  const [newRole, setNewRole] = useState<Role | null>(null);
  // Narrow screens show one pane at a time; home is the conversation.
  const [showList, setShowList] = useState(false);
  const [openingId, setOpeningId] = useState<string | null>(null);
  const booted = useRef(false);
  // The message a memory came from; the chat scrolls to and marks it.
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const [channelSource, setChannelSource] = useState<{ role: Role; source: ChannelMemorySource } | null>(null);
  const [previewRole, setPreviewRole] = useState<Role | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [profileTab, setProfileTab] = useState<"profile" | "accounts">("profile");
  const [settingsSection, setSettingsSection] = useState<SettingsSectionId | undefined>(returnedFromOwnerKnowledgeAuthorization ? "owner-knowledge" : undefined);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const searchTerm = query.trim().toLocaleLowerCase();
  const filteredRoles = roles.roles.filter((role) => `${role.name} ${role.description}`.toLocaleLowerCase().includes(searchTerm));

  useEffect(() => { if (searchOpen && showList && panel !== 'settings') searchRef.current?.focus(); }, [searchOpen, showList, panel]);

  const openChat = async (role: Role) => {
    roles.setError("");
    setOpeningId(role.id);
    const opened = await chat.open(role);
    setOpeningId(null);
    if (!opened) return null;
    writeLastRole(role.id);
    setHighlightedId(null);
    setView("chat");
    setShowList(false);
    return opened;
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
  const showProfile = (role: Role, tab: "profile" | "accounts" = "profile") => { roles.select(role); roles.startEdit(); setProfileTab(tab); setPanel("profile"); };
  const openTavern = () => { setView("tavern"); setShowList(false); if (panel === 'settings') setPanel(null); };
  const backToChat = () => { setView("chat"); setShowList(false); setHighlightedId(null); if (panel === 'settings') setPanel(null); };
  const openChannels = () => { setView("channels"); setShowList(false); setHighlightedId(null); if (panel === 'settings') setPanel(null); };
  const openNovelai = (roleId = studioRoleId ?? chat.roleId ?? roles.roles[0]?.id) => { if (roleId) setStudioRoleId(roleId); setView("novelai"); setShowList(false); if (panel === 'settings') setPanel(null); };

  const openMemorySource = async (role: Role, origin: MemoryOrigin, messageId?: string) => {
    chat.setError("");
    if (origin.sessionKey.startsWith("thread:")) {
      setChannelSource({ role, source: { origin, messageId } }); return;
    }
    if (origin.sessionKey !== `role:${role.id}`) { chat.setError("来源会话与当前角色不匹配"); setView("chat"); return; }
    const messages = await chat.open(role);
    setView("chat");
    if (!messages) return;
    const [from, to] = origin.messageRange ?? [];
    const source = messageId
      ? messages.find((message) => message.id === messageId)
      : messages.find((message) => from !== undefined && message.sequence >= from && message.sequence <= to!);
    if (!source) { chat.setError("来源消息当前不可访问"); return; }
    setHighlightedId(source.id);
  };
  const openTodoSource = async (roleId: string, sessionKey: string, messageId: string) => {
    const sourceRole = roles.roles.find((role) => role.id === roleId);
    if (!sourceRole || sessionKey !== `role:${roleId}`) {
      chat.setError("来源角色当前不可访问");
      setView("chat");
      return;
    }
    const messages = await openChat(sourceRole);
    if (!messages) return;
    if (!messages.some((message) => message.id === messageId)) {
      chat.setError("来源消息当前不可访问");
      return;
    }
    setHighlightedId(messageId);
  };
  const chatWith = (role: Role) => role.id === chat.roleId ? backToChat() : void openChat(role);

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
    roles.cancel();
    setPanel(null);
  };

  const deleteRole = async () => {
    const deletedId = roles.selected?.id;
    if (!await roles.remove()) return;
    setPanel(null);
    if (deletedId !== chat.roleId) return;
    chat.reset();
    // In the tavern the user stays with the cards; in a conversation, move on to the next role.
    const next = roles.roles.find((role) => role.id !== deletedId);
    if (next && view !== "tavern") void openChat(next);
  };

  // Switching roles mid-reply would mix two conversations, so the list waits for the reply.
  const lockSwitch = chat.sending || openingId !== null;

  return <div className={`shell${showList ? " show-list" : ""}`}>
    <nav className="app-rail" aria-label="页面">
      <button type="button" className="rail-search" aria-label="搜索角色" title="搜索角色" aria-expanded={searchOpen} aria-controls={searchOpen ? "role-search" : undefined} onClick={() => { if (panel === 'settings') setPanel(null); setSearchOpen(true); setShowList(true); searchRef.current?.focus(); }}><Icon name="search" size={21} /></button>
      <span className="rail-divider" aria-hidden="true" />
      <div className="side-tabs">
        <button type="button" aria-label="对话" title="对话" aria-current={view === "chat" || view === "memories" || view === "channels" ? "page" : undefined} onClick={backToChat}><Icon name="chat" size={22} /></button>
        <button type="button" aria-label="角色" title="角色" aria-current={view === "tavern" ? "page" : undefined} onClick={openTavern}><Icon name="people" size={22} /></button>
        <button type="button" aria-label="NovelAI 图片工作台" title="NovelAI 图片工作台" aria-current={view === "novelai" ? "page" : undefined} onClick={() => openNovelai()}><Icon name="image" size={22} /></button>
        {todoNavigation.enabled && <button type="button" className="todo-entry" aria-label={todoNavigation.unread ? `待办提醒 ${todoNavigation.unread}` : '待办'} title="待办"
          aria-haspopup="dialog" aria-expanded={todoOpen} onClick={() => { if (panel === 'settings') setPanel(null); setTodoOpen(true); }}>
          <Icon name="todo" size={22} />{todoNavigation.unread > 0 && <span>{todoNavigation.unread > 99 ? '99+' : todoNavigation.unread}</span>}
        </button>}
      </div>
      <footer className="rail-footer"><IconButton icon="settings" label="设置" onClick={() => { setSettingsSection(undefined); setPanel("settings"); }} /></footer>
    </nav>
    <aside className="sidebar" aria-label={view === "tavern" ? "角色管理" : view === "novelai" ? "工作台角色" : "对话列表"} aria-hidden={panel === 'settings' || undefined}
      ref={element => { if (element) element.inert = panel === 'settings'; }}>
      <div className="sidebar-tools">
        {view === "tavern" && <>
          <div className="tavern-actions">
            <button type="button" className="sidebar-create" aria-label="新建角色" onClick={startCreate}><Icon name="plus" size={18} /><span>新建</span></button>
            <button type="button" onClick={() => importRef.current?.click()} disabled={roles.busy}><Icon name="upload" size={17} /><span>导入角色卡</span></button>
            <input ref={importRef} type="file" className="image-file-input" aria-label="导入角色卡文件" accept=".json,.png,.apng,.charx,application/json,image/png" disabled={roles.busy} onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file) { startCreate(); void roles.previewImport(file); } }} />
          </div>
          <div className="all-roles"><Icon name="grid" size={17} /><span>全部角色</span><small>{roles.roles.length}</small></div>
        </>}
        {searchOpen && <div className="sidebar-search">
          <Icon name="search" size={16} />
          <input ref={searchRef} id="role-search" type="search" aria-label="搜索角色" placeholder="搜索角色…" value={query} onChange={(event) => setQuery(event.target.value)} />
          <IconButton icon="close" label="清除搜索" onClick={() => { setQuery(""); setSearchOpen(false); }} />
        </div>}
      </div>
      <div className="sidebar-body">
        {roles.error && panel === null && view !== "tavern" && <Notice onDismiss={() => roles.setError("")}>{roles.error}</Notice>}
        <RoleList roles={filteredRoles} loading={roles.loading} loadFailed={roles.loadFailed} searching={Boolean(searchTerm)} activeId={view === "tavern" ? null : view === "novelai" ? studioRoleId : chat.roleId} disabled={view !== "novelai" && lockSwitch} onOpen={view === "novelai" ? (role) => openNovelai(role.id) : chatWith} />
      </div>
    </aside>

    <main className="main" aria-hidden={panel === 'settings' || undefined} ref={element => { if (element) element.inert = panel === 'settings'; }}>
      {view === "tavern"
        ? <TavernView roles={roles.roles} query={searchTerm} loading={roles.loading} error={panel === null ? roles.error : ""} onDismissError={() => roles.setError("")} chatDisabled={lockSwitch} onBack={() => setShowList(true)} onShow={showProfile} onChat={chatWith} onPreviewAvatar={setPreviewRole} />
        : view === "novelai"
        ? <NovelaiStudio roles={roles.roles} selectedRoleId={studioRoleId ?? chat.roleId ?? roles.roles[0]?.id} onRoleChange={setStudioRoleId}
          onGenerated={chat.refresh} settingsOpen={panel === 'settings'} onOpenSettings={() => { setSettingsSection('novelai'); setPanel('settings'); }} />
        : view === "memories" && chatRole
        ? <MemoryPanel key={chatRole.id} role={chatRole} onBack={backToChat} onOpenSource={(origin, messageId) => void openMemorySource(chatRole, origin, messageId)} />
        : view === "channels" && chatRole
        ? <ChannelView role={chatRole} onBack={backToChat} onAccounts={() => showProfile(chatRole, "accounts")} />
        : chatRole
        ? <ChatView role={chatRole} chat={chat} highlightedId={highlightedId} onBack={() => setShowList(true)} onShowProfile={() => showProfile(chatRole)} onShowMemories={() => setView("memories")} onShowChannels={openChannels} />
        : <WelcomeView loading={roles.loading || openingId !== null} hasRoles={roles.roles.length > 0} error={chat.error} onDismissError={() => chat.setError("")} onCreate={startCreate} onBack={() => setShowList(true)} />}
    </main>

    {todoOpen && <TodoDrawer onClose={() => setTodoOpen(false)} onOpenSource={(roleId, sessionKey, messageId) => { setTodoOpen(false); void openTodoSource(roleId, sessionKey, messageId); }} />}
    {channelSource && <Dialog modalFocus title="记忆来源" onClose={() => setChannelSource(null)}>
      <ChannelView key={`${channelSource.role.id}:${channelSource.source.origin.stableSourceKey}:${channelSource.source.messageId ?? ""}`} role={channelSource.role} sourceOnly onBack={() => setChannelSource(null)} initialSource={channelSource.source} />
    </Dialog>}

    {panel === "create" && <Dialog title="创建角色" variant="page" onClose={closePanel}>
      <CreateRolePage state={roles} number={roles.roles.length + 1} onSubmit={() => void saveRole()} onCancel={cancelEdit} />
    </Dialog>}
    {panel === "profile" && <Dialog title="角色资料" variant="page" onClose={closePanel}>
      <div className="role-detail">
        <div className="role-detail-tabs" role="tablist" aria-label="角色详情">
          <button type="button" role="tab" aria-selected={profileTab === "profile"} onClick={() => setProfileTab("profile")}>资料</button>
          <button type="button" role="tab" aria-selected={profileTab === "accounts"} onClick={() => setProfileTab("accounts")}>账号</button>
        </div>
        <div className="role-detail-panel" role="tabpanel" aria-label={profileTab === "profile" ? "资料" : "账号"}>
          {profileTab === "profile" ? <CreateRolePage state={roles} number={roles.roles.findIndex((role) => role.id === roles.selected?.id) + 1} onSubmit={() => void saveRole()} onCancel={cancelEdit} onDelete={() => void deleteRole()} />
            : roles.selected && <RoleAccountsPanel key={roles.selected.id} roleId={roles.selected.id} onOpenSettings={() => { roles.cancel(); setSettingsSection("channels"); setPanel("settings"); }} />}
        </div>
      </div>
    </Dialog>}
    {panel === "settings" && <SettingsDialog initialSection={settingsSection} onRoleSaved={roles.receiveRole} onClose={() => setPanel(null)} />}
    {previewRole && <Dialog title={`${previewRole.name}头像`} variant="modal" onClose={() => setPreviewRole(null)}>
      <div className="avatar-lightbox"><img src={previewRole.avatarOriginalUrl ?? previewRole.avatarUrl ?? ""} alt={`${previewRole.name}头像`} /></div>
    </Dialog>}
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
