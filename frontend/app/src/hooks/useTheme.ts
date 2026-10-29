import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";

function apply(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(() => {
    try {
      const saved = localStorage.getItem("agentos_theme");
      return saved === "light" || saved === "dark" ? saved : "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => apply(choice), [choice]);
  const update = useCallback((next: ThemeChoice) => {
    try {
      if (next === "system") localStorage.removeItem("agentos_theme");
      else localStorage.setItem("agentos_theme", next);
    } catch {
      /* storage unavailable */
    }
    setChoice(next);
  }, []);
  return [choice, update];
}
