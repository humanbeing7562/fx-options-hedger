// One button that cycles the colour theme: Auto (follow the system) -> Light -> Dark.

const NEXT = { auto: "light", light: "dark", dark: "auto" };
const LABEL = { auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark" };

export default function ThemeToggle({ theme, onChange }) {
  return (
    <button
      className="theme-toggle"
      onClick={() => onChange(NEXT[theme])}
      title="Colour theme: Auto follows your system setting. Click to change."
      aria-label={`Colour theme: ${theme}. Click to change.`}
    >
      {LABEL[theme]}
    </button>
  );
}