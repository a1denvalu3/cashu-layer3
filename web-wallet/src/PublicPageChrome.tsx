import { ArrowUpRight, Code2, Moon, Sun } from "lucide-react";
import { useTheme } from "./theme";
import { TmpCashMark } from "./TmpCashMark";

export function WalletLink() {
  return (
    <a className="button dark landing-open" href="/wallet/">
      Open wallet <ArrowUpRight size={17} aria-hidden="true" />
    </a>
  );
}

export function PublicPageHeader({ cryptography = false }) {
  const { theme, changePreference } = useTheme();
  return (
    <header className="header landing-header">
      <a className="brand" href="/" aria-label="tmp.cash home">
        <span className="brand-mark">
          <TmpCashMark size={25} strokeWidth={1.6} aria-hidden="true" />
        </span>
        tmp<span>.cash</span>
      </a>
      <nav aria-label="Main navigation">
        <a href={cryptography ? "/#how-it-works" : "#how-it-works"}>
          How it works
        </a>
        <a
          href="/how-it-works/?view=cryptography"
          aria-current={cryptography ? "page" : undefined}
        >
          Cryptography
        </a>
      </nav>
      <div className="header-tools">
        <button
          className="icon-button theme-toggle"
          aria-label={theme === "dark" ? "Use light mode" : "Use night mode"}
          onClick={() => changePreference(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? (
            <Sun size={19} aria-hidden="true" />
          ) : (
            <Moon size={19} aria-hidden="true" />
          )}
        </button>
        <WalletLink />
      </div>
    </header>
  );
}

export function PublicPageFooter() {
  return (
    <footer className="landing-footer">
      <span>
        <span className="status-dot" /> tmp.cash · EXPERIMENTAL ECASH
      </span>
      <a className="text-button" href="/how-it-works/?view=cryptography">
        The cryptography <ArrowUpRight size={13} aria-hidden="true" />
      </a>
      <a href="https://github.com/a1denvalu3/tmp.cash" className="text-button">
        <Code2 size={15} aria-hidden="true" /> View the source{" "}
        <ArrowUpRight size={13} aria-hidden="true" />
      </a>
    </footer>
  );
}
