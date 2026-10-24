import { describe, expect, it } from "vitest";
import type { Role } from "../../api/types";
import { draftFromRole, payloadFromDraft, readCreateDraft, writeCreateDraft } from "./draft";

const provenance = { format: "json" as const, source: "mio.json", importedAt: "2026-10-23T00:00:00Z" };
const role: Role = {
  id: "role-1",
  name: "待办角色",
  description: "",
  profile: { version: 1, character: { profile: "设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" }, importProvenance: provenance },
  agentConfig: {
    enabledPlugins: ["todo", "calendar"],
    grantedCapabilities: ["todo.read", "calendar.read"],
    enabledTools: ["todo.search", "calendar.search"],
    browserUse: { enabled: true },
  },
  createdAt: "2026-10-08T00:00:00Z",
  updatedAt: "2026-10-08T00:00:00Z",
};

describe("role draft capability persistence", () => {
  it("clears the todo grant when disabled and preserves other capabilities", () => {
    const draft = draftFromRole(role);
    draft.todoSearchEnabled = false;

    expect(payloadFromDraft(draft).agentConfig).toEqual({
      memoryRecall: { enabled: false },
      browserUse: { enabled: true },
      enabledPlugins: ["calendar"],
      grantedCapabilities: ["calendar.read"],
      enabledTools: ["calendar.search"],
    });
  });
});

it("reads the versioned character profile and retains import provenance in the saved payload", () => {
  const draft = draftFromRole(role);
  draft.personality = "温柔";

  expect(draft.profile).toBe("设定");
  expect(payloadFromDraft(draft).profile).toEqual({
    version: 1,
    character: { profile: "设定", personality: "温柔", behaviorRules: "", responseConstraints: "", nickname: "" },
    importProvenance: provenance,
  });
});

it("restores versioned profile drafts and provenance while still reading old flat drafts", () => {
  const draft = draftFromRole(role);
  draft.profile = "背景";
  draft.personality = "谨慎";
  writeCreateDraft(draft);
  const restored = readCreateDraft();

  expect(restored).toMatchObject({ profile: "背景", personality: "谨慎", importProvenance: provenance });
  sessionStorage.setItem("meido:create-role-draft", JSON.stringify({ profile: "旧草稿设定", nickname: "主人" }));
  expect(readCreateDraft()).toMatchObject({ profile: "旧草稿设定", nickname: "主人", importProvenance: null });
});
