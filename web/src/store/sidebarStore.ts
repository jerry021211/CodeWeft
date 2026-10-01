import { create } from "zustand";

const storageKey = "codeagent.collapsed-workspaces";
function loadCollapsed(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(storageKey) ?? "[]");
    return Array.isArray(value) ? value.filter((key): key is string => typeof key === "string") : [];
  } catch {
    return [];
  }
}

// Both the desktop sidebar and mobile drawer share the same expansion state.
export const useSidebarStore = create<{
  collapsed: string[];
  setCollapsed: (key: string, collapsed: boolean) => void;
}>((set) => ({
  collapsed: loadCollapsed(),
  setCollapsed: (key, collapsed) => set((state) => {
    if (state.collapsed.includes(key) === collapsed) return state;
    const keys = collapsed ? [...state.collapsed, key] : state.collapsed.filter((item) => item !== key);
    try { localStorage.setItem(storageKey, JSON.stringify(keys)); } catch { /* Keep working without browser storage. */ }
    return { collapsed: keys };
  }),
}));
