import React, { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/dm-sans/600.css";
import "@fontsource/dm-sans/700.css";
import "@fontsource/dm-mono/400.css";
import "./style.css";
import "./theme";

const Wallet = lazy(() =>
  import("./App").then(({ App }) => ({ default: App })),
);
const Landing = lazy(() =>
  import("./LandingPage").then(({ LandingPage }) => ({ default: LandingPage })),
);
const Cryptography = lazy(() =>
  import("./CryptographyPage").then(({ CryptographyPage }) => ({
    default: CryptographyPage,
  })),
);
const root = document.getElementById("root")!;
const isWallet = root.dataset.page === "wallet";
const isCryptography = root.dataset.page === "cryptography";

// Some static hosts serve the landing entry before normalizing a directory URL.
if (["/wallet", "/how-it-works"].includes(window.location.pathname)) {
  window.location.replace(
    `${window.location.pathname}/${window.location.search}${window.location.hash}`,
  );
} else {
  createRoot(root).render(
    <React.StrictMode>
      <Suspense
        fallback={
          <p className="page-loading" role="status">
            Opening tmp.cash…
          </p>
        }
      >
        {isWallet ? (
          <Wallet />
        ) : isCryptography ? (
          <Cryptography />
        ) : (
          <Landing />
        )}
      </Suspense>
    </React.StrictMode>,
  );
}
