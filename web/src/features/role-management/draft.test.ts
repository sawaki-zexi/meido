import { describe, expect, it } from "vitest";
import type { Role } from "../../api/types";
import { draftFromRole, payloadFromDraft } from "./draft";

const role: Role = {
  id: "role-1",
  name: "待办角色",
  description: "",
  profile: { profile: "设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
  agentConfig: {
    enabledPlugins: ["todo", "calendar"],
    grantedCapabilities: ["todo.read", "calendar.read"],
    enabledTools: ["todo.search", "calendar.search"],
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
      enabledPlugins: ["calendar"],
      grantedCapabilities: ["calendar.read"],
      enabledTools: ["calendar.search"],
    });
  });
});
