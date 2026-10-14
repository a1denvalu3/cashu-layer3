import { useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
const preferenceKey = "layer3-theme";

function savedPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(preferenceKey);
    if (value === "light" || value === "dark") return value;
  } catch {
    // Browser preferences are optional.
  }
  return "system";
}

function resolveTheme(preference: ThemePreference) {
  return preference === "system"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light"
    : preference;
}

function applyTheme(theme: "light" | "dark") {
  document.documentElement.dataset.theme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "dark" ? "#171b16" : "#f5f3ed");
}

// Apply the saved theme before React mounts to avoid a light wallet flashing.
applyTheme(resolveTheme(savedPreference()));

export function useTheme() {
  const [preference, setPreference] =
    useState<ThemePreference>(savedPreference);
  const [theme, setTheme] = useState(() => resolveTheme(preference));

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => {
      const resolved = resolveTheme(preference);
      applyTheme(resolved);
      setTheme(resolved);
    };
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [preference]);

  function changePreference(value: ThemePreference) {
    setPreference(value);
    const resolved = resolveTheme(value);
    applyTheme(resolved);
    setTheme(resolved);
    try {
      localStorage.setItem(preferenceKey, value);
    } catch {
      // The selected theme still works for this session.
    }
  }

  return { preference, theme, changePreference };
}
