import { beforeAll, afterAll, describe, it, expect } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import "fake-indexeddb/auto";
import {
  PSWallet,
  psRandomHex,
  psPreimageHash,
  psTokenAmount,
  psNullifier,
  encodePSToken,
  decodePSToken,
  checkPSBacking,
  type PSToken,
} from "@cashu/cashu-ts/layer3";
import { Layer3Manager } from "coco-cashu-core/layer3";
import { Layer3IndexedDbRepository } from "coco-cashu-indexeddb/layer3";
import { denominationKeysetId } from "../packages/cashu-ts/src/layer3/crypto";

let process: ChildProcess, mintUrl: string, wallet: PSWallet;
const repos: Layer3IndexedDbRepository[] = [];
async function control(path: string, data: unknown) {
  const r = await fetch(mintUrl + "/test/" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
async function mint(value: number): Promise<PSToken> {
  const preimage = psRandomHex(),
    quote = await wallet.createMintQuote(value, psPreimageHash(preimage));
  await control("accept", { payment_hash: quote.payment_hash });
  return wallet.finishMint(
    wallet.prepareMint(await wallet.getMintQuote(quote.quote), preimage),
  );
}
function repository() {
  const repo = new Layer3IndexedDbRepository("test-" + psRandomHex());
  repos.push(repo);
  return repo;
}
beforeAll(async () => {
  const port = await new Promise<number>((resolve) => {
    const s = createServer();
    s.listen(0, "127.0.0.1", () => {
      const a = s.address();
      if (a && typeof a !== "string") s.close(() => resolve(a.port));
    });
  });
  mintUrl = `http://127.0.0.1:${port}`;
  process = spawn(
    "../.venv/bin/python",
    ["tests/fixture.py", "--port", String(port)],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let errors = "";
  process.stderr?.on("data", (d) => {
    errors += d.toString();
  });
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(mintUrl + "/test/health")).ok) break;
    } catch {
      /* starting */
    }
    if (process.exitCode !== null) throw new Error(errors);
    await new Promise((r) => setTimeout(r, 100));
  }
  wallet = new PSWallet(mintUrl);
  await wallet.loadMint();
}, 30000);
afterAll(async () => {
  process?.kill("SIGTERM");
  for (const repo of repos) await repo.delete();
});

describe("Python mint ↔ browser cashu-ts protocol", () => {
  it("bypasses ngrok warnings for mint reads and quote requests", async () => {
    const requests: { method: string; headers: Headers }[] = [];
    const tunneled = new PSWallet(
      "https://mint.ngrok-free.app",
      async (input, init) => {
        requests.push({
          method: init?.method ?? "GET",
          headers: new Headers(init?.headers),
        });
        return fetch(mintUrl + new URL(String(input)).pathname, init);
      },
    );
    await tunneled.loadMint();
    await tunneled.createMintQuote(32, psPreimageHash(psRandomHex()));
    expect(requests.some((r) => r.method === "POST")).toBe(true);
    expect(requests.some((r) => r.method === "GET")).toBe(true);
    for (const request of requests)
      expect(request.headers.get("ngrok-skip-browser-warning")).toBe("1");
    const normalRequests: Headers[] = [];
    const normal = new PSWallet(mintUrl, async (input, init) => {
      normalRequests.push(new Headers(init?.headers));
      return fetch(input, init);
    });
    await normal.loadMint();
    for (const headers of normalRequests)
      expect(headers.has("ngrok-skip-browser-warning")).toBe(false);
  });
  it("reads the mint lifetime policy and supports mints without block settings", async () => {
    expect(wallet.defaultTokenLifetimeBlocks).toBeNull();
    expect(wallet.fundingHtlcCltvDelta).toBeNull();
    const configured = new PSWallet(mintUrl, async (input, init) => {
      const response = await fetch(input, init);
      if (String(input).endsWith("/v1/info")) {
        return new Response(
          JSON.stringify({
            ...(await response.json()),
            default_token_lifetime_blocks: 2016,
            funding_htlc_cltv_delta: 2040,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return response;
    });
    await configured.loadMint();
    expect(configured.defaultTokenLifetimeBlocks).toBe(2016);
    expect(configured.fundingHtlcCltvDelta).toBe(2040);
    expect(configured.keysetId).toBe(wallet.keysetId);
  });
  it("issues and verifies denomination credentials and privately swaps many to many", async () => {
    const token = await mint(1000);
    expect(token.proofs.map((p) => p.amount)).toEqual([
      512, 256, 128, 64, 32, 8,
    ]);
    expect(decodePSToken(encodePSToken(token))).toEqual(token);
    const op = await wallet.prepareSwap(
      token,
      [512, 256, 128, 64, 16, 8, 8, 8],
    );
    expect(JSON.stringify(op.request)).not.toContain(
      token.proofs[0].payment_hash,
    );
    expect(JSON.stringify(op.request)).not.toContain(token.proofs[0].preimage);
    expect(op.request.inputs.every((p) => p.presentation.length === 770)).toBe(
      true,
    );
    const result = await wallet.finishSwap(op);
    expect(psTokenAmount(result)).toBe(1000);
    expect(await wallet.finishSwap(op)).toEqual(result);
    expect(await wallet.checkProofs(token.proofs)).toEqual(
      token.proofs.map(() => "SPENT"),
    );
    expect(await wallet.checkProofs(result.proofs)).toEqual(
      result.proofs.map(() => "UNSPENT"),
    );
    const changed = {
      ...result,
      proofs: [{ ...result.proofs[0], backing_amount: 999 }],
    };
    expect(() => wallet.verifyToken(changed)).toThrow();
  });
  it("rejects mixed-HTLC swaps and returns correctly allocated combined-melt fee change", async () => {
    const first = await mint(25),
      second = await mint(37);
    const combined: PSToken = {
      version: 3,
      mint: mintUrl,
      proofs: [...first.proofs, ...second.proofs],
    };
    await expect(wallet.prepareSwap(combined)).rejects.toThrow("exactly one");
    const { request } = await control("receiver", { amount: 40 });
    const quote = await wallet.createMeltQuote(request);
    const op = await wallet.prepareMelt(combined, quote),
      result = await wallet.finishMelt(op);
    expect(result.receipt.fee).toBe(2);
    expect(result.receipt.fee_msat).toBe(1500);
    expect(result.token && psTokenAmount(result.token)).toBe(20);
    expect(
      result.token?.proofs.every(
        (p) => p.payment_hash === second.proofs[0].payment_hash,
      ),
    ).toBe(true);
    expect(
      (await wallet.pendingHTLCs()).htlcs.map((p) => p.payment_hash),
    ).not.toContain(first.proofs[0].payment_hash);
    expect(
      (await wallet.pendingHTLCs()).htlcs.map((p) => p.payment_hash),
    ).toContain(second.proofs[0].payment_hash);
    expect(await wallet.finishMelt(op)).toEqual(result);
  });
  it("rejects duplicate proofs, malformed tokens and missing preimages", async () => {
    const token = await mint(32);
    expect(() =>
      encodePSToken({ ...token, proofs: [token.proofs[0], token.proofs[0]] }),
    ).toThrow("Duplicate");
    expect(() => decodePSToken("cashuPS!")).toThrow();
    expect(() =>
      wallet.verifyToken({
        ...token,
        proofs: [{ ...token.proofs[0], amount: 16 }],
      }),
    ).toThrow();
    const { request } = await control("receiver", { amount: 10 });
    const quote = await wallet.createMeltQuote(request);
    await expect(
      wallet.prepareMelt(
        {
          ...token,
          proofs: token.proofs.map((p) => ({ ...p, preimage: null })),
        },
        quote,
      ),
    ).rejects.toThrow("missing");
  });
});

describe("Coco operation journal and IndexedDB", () => {
  it("uses the mint's fee quote and returns its unused reserve as change", async () => {
    await control("fee-policy", { fee_limit_sat: 5 });
    try {
      const repo = repository(),
        quoteRequests: unknown[] = [],
        manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
          if (String(input).endsWith("/v1/melt/quote/bolt11"))
            quoteRequests.push(JSON.parse(String(init?.body)));
          return fetch(input, init);
        });
      await manager.init();
      const funding = await manager.createFunding(100);
      await control("accept", { payment_hash: funding.id });
      await manager.pollFunding(funding.id);
      const tooLarge = await control("receiver", { amount: 96 });
      const tooLargeQuote = await manager.meltQuote(tooLarge.request);
      expect(tooLargeQuote.fee_reserve).toBe(5);
      await expect(manager.melt(tooLargeQuote)).rejects.toThrow("Not enough");
      expect((await repo.read()).operations).toEqual([]);
      const payable = await control("receiver", { amount: 95 });
      const quote = await manager.meltQuote(payable.request);
      expect(quote.fee_reserve).toBe(5);
      const receipt = await manager.melt(quote);
      expect(receipt.amount).toBe(95);
      expect(receipt.fee).toBe(2);
      expect((await manager.view()).balance).toBe(3);
      expect(quoteRequests).toEqual([
        { request: tooLarge.request },
        { request: payable.request },
      ]);
    } finally {
      await control("fee-policy", { fee_limit_sat: 2 });
    }
  });
  it("reconciles a pending melt automatically using its exact saved request", async () => {
    const repo = repository(),
      requests: string[] = [];
    let pending = true;
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      const response = await fetch(input, init);
      if (String(input).endsWith("/v1/melt/bolt11")) {
        requests.push(String(init?.body));
        if (pending) {
          pending = false;
          expect(response.ok).toBe(true);
          const state = await repo.read();
          expect(state.operations).toHaveLength(1);
          expect(
            state.proofs.filter((p) => p.state === "reserved"),
          ).not.toHaveLength(0);
          return new Response(
            JSON.stringify({
              detail: "melt payment is pending; retry the same request",
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
      }
      return response;
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const quote = await manager.meltQuote(request);
    expect((await manager.melt(quote)).amount).toBe(10);
    expect(requests).toHaveLength(2);
    expect(requests[0]).toBe(requests[1]);
    expect((await repo.read()).operations).toEqual([]);
    expect((await manager.view()).balance).toBe(88);
  });
  it("keeps a long-running melt reserved after bounded automatic checks", async () => {
    const repo = repository(),
      requests: string[] = [];
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      if (String(input).endsWith("/v1/melt/bolt11")) {
        requests.push(String(init?.body));
        return new Response(
          JSON.stringify({
            detail: "melt payment is pending; retry the same request",
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      return fetch(input, init);
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    await expect(
      manager.melt(await manager.meltQuote(request)),
    ).rejects.toThrow("pending");
    expect(requests.length).toBeGreaterThan(1);
    expect(requests.length).toBeLessThanOrEqual(5);
    expect(new Set(requests).size).toBe(1);
    const state = await repo.read();
    expect(state.operations).toHaveLength(1);
    expect(
      state.proofs
        .filter((p) => p.state === "reserved")
        .reduce((n, p) => n + p.proof.amount, 0),
    ).toBe(64);
    expect(state.activity.some((a) => a.purpose === "melt")).toBe(false);
  });
  it("does not automatically retry an unavailable response containing pending text", async () => {
    const repo = repository();
    let attempts = 0;
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      if (String(input).endsWith("/v1/melt/bolt11")) {
        attempts++;
        return new Response(
          JSON.stringify({
            detail: "melt payment is pending; retry the same request",
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        );
      }
      return fetch(input, init);
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    await expect(
      manager.melt(await manager.meltQuote(request)),
    ).rejects.toThrow("pending");
    expect(attempts).toBe(1);
    expect((await repo.read()).operations).toHaveLength(1);
    expect((await manager.view()).reserved).toBe(64);
  });
  it("journals every backing of a combined receive before its first swap", async () => {
    const first = await mint(32),
      second = await mint(64),
      combined: PSToken = {
        version: 3,
        mint: mintUrl,
        proofs: [...first.proofs, ...second.proofs],
      },
      repo = repository();
    let dropped = false;
    const requests: string[] = [];
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      const response = await fetch(input, init);
      if (String(input).endsWith("/v1/swap")) {
        requests.push(String(init?.body));
        if (!dropped) {
          dropped = true;
          throw new TypeError("Lost first combined-receive response");
        }
      }
      return response;
    });
    await manager.init();
    await expect(manager.receive(encodePSToken(combined))).rejects.toThrow(
      "Lost first",
    );
    const saved = await repo.read();
    expect(saved.operations).toHaveLength(2);
    expect(saved.proofs.reduce((sum, p) => sum + p.proof.amount, 0)).toBe(96);
    expect(saved.proofs.every((p) => p.state === "reserved")).toBe(true);
    await expect(manager.receive(encodePSToken(combined))).rejects.toThrow(
      "retry them under Pending operations",
    );
    const restarted = new Layer3Manager(repo, mintUrl);
    await restarted.init();
    for (const op of saved.operations) await restarted.retry(op.id);
    expect((await restarted.view()).balance).toBe(96);
    expect((await repo.read()).operations).toEqual([]);
  });
  it("recovers a saved issuance receipt even after the funding quote closes", async () => {
    const repo = repository();
    let dropped = false;
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      const response = await fetch(input, init);
      if (String(input).endsWith("/v1/mint/bolt11") && !dropped) {
        dropped = true;
        throw new TypeError("Lost issuance receipt");
      }
      return response;
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await expect(manager.pollFunding(funding.id)).rejects.toThrow("Lost");
    await control("settle", { payment_hash: funding.id });
    const restarted = new Layer3Manager(repo, mintUrl);
    await restarted.init();
    expect((await restarted.pollFunding(funding.id)).state).toBe("minted");
    expect((await repo.read()).operations).toEqual([]);
    expect((await repo.read()).activity).toHaveLength(1);
    expect((await restarted.view()).balance).toBe(0);
    expect((await restarted.view()).expired).toBe(100);
  });
  it("rolls back the entire receive batch if a later group is already reserved", async () => {
    const repo = repository(),
      first = await mint(32),
      second = await mint(64);
    const entries = [];
    for (const token of [first, second]) {
      const payload = await wallet.prepareSwap(token);
      entries.push({
        operation: {
          id: payload.id,
          mint: mintUrl,
          payload,
          purpose: "receive" as const,
          sendCount: 0,
          amount: psTokenAmount(token),
          createdAt: Date.now(),
          error: null,
        },
        inputs: token.proofs.map((proof) => ({
          id: psNullifier(proof),
          mint: mintUrl,
          proof,
          state: "ready" as const,
          operationId: null,
          createdAt: Date.now(),
        })),
      });
    }
    await repo.reserve(entries[1].operation, entries[1].inputs);
    await expect(repo.reserveMany(entries)).rejects.toThrow("already exists");
    expect((await repo.read()).operations.map((op) => op.id)).toEqual([
      entries[1].operation.id,
    ]);
    expect((await repo.read()).proofs.map((p) => p.id)).toEqual(
      entries[1].inputs.map((p) => p.id),
    );
    expect(await wallet.checkProofs(first.proofs)).toEqual(["UNSPENT"]);
  });
  it("coalesces slow background refreshes so they cannot crowd out spending", async () => {
    const repo = repository();
    let slow = false,
      snapshots = 0;
    let started!: () => void, resume!: () => void;
    const pending = new Promise<void>((resolve) => (started = resolve));
    const released = new Promise<void>((resolve) => (resume = resolve));
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      if (slow && String(input).endsWith("/v1/htlcs/pending")) {
        snapshots++;
        if (snapshots === 1) {
          started();
          await released;
        }
      }
      return fetch(input, init);
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    slow = true;
    const refreshing = manager.refresh();
    await pending;
    const repeated = Array.from({ length: 12 }, () => manager.refresh());
    const sending = manager.send(10);
    resume();
    await Promise.all([refreshing, ...repeated, sending]);
    // One shared background read, plus send verification, swap preparation and final refresh.
    expect(snapshots).toBe(4);
    expect((await manager.view()).balance).toBe(90);
  });
  it("queues background refreshes while a deposit is being minted", async () => {
    const repo = repository();
    let snapshots = 0;
    let mintStarted!: () => void, resumeMint!: () => void;
    const started = new Promise<void>((resolve) => (mintStarted = resolve));
    const resume = new Promise<void>((resolve) => (resumeMint = resolve));
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/htlcs/pending")) snapshots++;
      const response = await fetch(input, init);
      if (url.endsWith("/v1/mint/bolt11")) {
        mintStarted();
        await resume;
      }
      return response;
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    const minting = manager.pollFunding(funding.id);
    await started;
    const before = snapshots,
      refreshing = manager.refresh();
    await Promise.resolve();
    await Promise.resolve();
    const during = snapshots;
    resumeMint();
    await Promise.all([minting, refreshing]);
    expect(during).toBe(before);
    expect((await manager.view()).balance).toBe(100);
    expect((await repo.read()).operations).toEqual([]);
  });
  it("recovers falsely expired deposits only after live backing and unspent proof checks", async () => {
    const repo = repository(),
      manager = new Layer3Manager(repo, mintUrl);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const original = (await repo.read()).proofs;
    for (const p of original)
      await repo.proofs.update(p.id, { state: "expired" });
    const restarted = new Layer3Manager(repo, mintUrl);
    await restarted.init();
    expect((await restarted.view()).balance).toBe(100);
    expect((await restarted.view()).expired).toBe(0);
    expect((await repo.read()).proofs.map((p) => p.proof)).toEqual(
      original.map((p) => p.proof),
    );

    // Another wallet can spend a saved backup's proofs while their HTLC stays live.
    await wallet.finishSwap(
      await wallet.prepareSwap({
        version: 3,
        mint: mintUrl,
        proofs: original.map((p) => p.proof),
      }),
    );
    for (const p of original)
      await repo.proofs.update(p.id, { state: "expired" });
    await restarted.refresh();
    expect((await restarted.view()).balance).toBe(0);
    expect((await repo.read()).proofs.every((p) => p.state === "spent")).toBe(
      true,
    );
  });
  it("keeps outgoing tokens excluded when recovering live change and never restores closed backing", async () => {
    const repo = repository(),
      manager = new Layer3Manager(repo, mintUrl);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    await manager.send(10);
    const live = (await repo.read()).proofs.filter((p) =>
      ["ready", "sent"].includes(p.state),
    );
    for (const p of live) await repo.proofs.update(p.id, { state: "expired" });
    const restarted = new Layer3Manager(repo, mintUrl);
    await restarted.init();
    expect((await restarted.view()).balance).toBe(90);
    expect((await restarted.view()).sent).toBe(10);
    expect((await restarted.view()).expired).toBe(0);
    await control("settle", { payment_hash: funding.id });
    await restarted.refresh();
    await restarted.refresh();
    expect((await restarted.view()).balance).toBe(0);
    expect((await restarted.view()).expired).toBe(100);
  });
  it("keeps falsely expired tokens unavailable when spend checks fail or report pending", async () => {
    const repo = repository();
    let check: "normal" | "offline" | "pending" = "normal";
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      if (String(input).endsWith("/v1/checkstate")) {
        if (check === "offline") return new Response("", { status: 503 });
        if (check === "pending")
          return new Response(
            JSON.stringify({
              states: JSON.parse(String(init?.body)).nullifiers.map(
                () => "PENDING",
              ),
            }),
            { headers: { "Content-Type": "application/json" } },
          );
      }
      return fetch(input, init);
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    for (const p of (await repo.read()).proofs)
      await repo.proofs.update(p.id, { state: "expired" });
    check = "offline";
    await expect(manager.refresh()).rejects.toThrow();
    expect((await manager.view()).balance).toBe(0);
    expect((await manager.view()).expired).toBe(100);
    expect((await manager.view()).verified).toBe(false);
    check = "pending";
    await manager.refresh();
    expect((await manager.view()).balance).toBe(0);
    expect((await manager.view()).reserved).toBe(100);
    check = "normal";
    await manager.refresh();
    expect((await manager.view()).balance).toBe(100);
  });
  it("preserves and expires old claims when their backing closes and the mint keys change", async () => {
    const repo = repository(),
      manager = new Layer3Manager(repo, mintUrl);
    await manager.init();
    const funding = await manager.createFunding(32);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const saved = (await repo.read()).proofs[0];
    const replacement = new Layer3Manager(
      repo,
      mintUrl,
      async (input, init) => {
        const response = await fetch(input, init);
        if (!String(input).endsWith("/v1/keys")) return response;
        const current = await response.json(),
          keys = {
            ...current.keys,
            "1": current.keys["2"],
            "2": current.keys["1"],
          };
        return new Response(
          JSON.stringify({ ...current, id: denominationKeysetId(keys), keys }),
          {
            headers: { "Content-Type": "application/json" },
          },
        );
      },
    );
    // Live claims must still pass signature/keyset verification.
    await expect(replacement.init()).rejects.toThrow();
    expect((await replacement.view()).verified).toBe(false);
    expect((await repo.read()).proofs[0].state).toBe("ready");

    await control("settle", { payment_hash: funding.id });
    await replacement.init();
    const view = await replacement.view(),
      expired = (await repo.read()).proofs[0];
    expect(view.verified).toBe(true);
    expect(view.balance).toBe(0);
    expect(view.expired).toBe(32);
    expect(expired.state).toBe("expired");
    expect(expired.proof).toEqual(saved.proof);
  });
  it("recovers an identical private-swap request after a lost response and reload", async () => {
    const repo = repository();
    let dropped = false;
    const requests: string[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).endsWith("/v1/swap")) {
        requests.push(String(init?.body));
        const response = await fetch(input, init);
        if (!dropped) {
          dropped = true;
          throw new TypeError("Simulated lost response");
        }
        return response;
      }
      return fetch(input, init);
    };
    const alice = new Layer3Manager(repo, mintUrl, fetcher);
    await alice.init();
    const funding = await alice.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await alice.pollFunding(funding.id);
    expect((await alice.view()).balance).toBe(100);
    await expect(alice.send(10)).rejects.toThrow("lost response");
    const saved = (await repo.read()).operations[0];
    expect(saved.purpose).toBe("send");
    expect((await alice.view()).balance).toBe(36);
    const reopened = new Layer3IndexedDbRepository(repo.name),
      restart = new Layer3Manager(reopened, mintUrl, fetcher);
    await restart.init();
    const result = await restart.retry(saved.id);
    expect(result.token && psTokenAmount(decodePSToken(result.token))).toBe(10);
    expect(requests[0]).toBe(requests[1]);
    expect((await restart.view()).balance).toBe(90);
    expect((await reopened.read()).operations).toEqual([]);
    const bobRepo = repository(),
      bob = new Layer3Manager(bobRepo, mintUrl);
    await bob.init();
    expect(await bob.receive(result.token!)).toBe(10);
    expect((await bob.view()).balance).toBe(10);
    await expect(bob.receive(result.token!)).rejects.toThrow("spent");
    await control("settle", { payment_hash: funding.id });
    await bob.refresh();
    expect((await bob.view()).balance).toBe(0);
    expect((await bob.view()).expired).toBe(10);
    reopened.close();
  });
  it("prevents competing operations from reserving the same inputs", async () => {
    const repo = repository(),
      token = await mint(32),
      payload = await wallet.prepareSwap(token);
    const op = {
      id: payload.id,
      mint: mintUrl,
      payload,
      purpose: "receive" as const,
      sendCount: 0,
      amount: 32,
      createdAt: Date.now(),
      error: null,
    };
    const inputs = token.proofs.map((proof) => ({
      id: psNullifier(proof),
      mint: mintUrl,
      proof,
      state: "ready" as const,
      operationId: null,
      createdAt: Date.now(),
    }));
    const otherId = psRandomHex();
    const other = { ...op, id: otherId, payload: { ...payload, id: otherId } };
    const results = await Promise.allSettled([
      repo.reserve(op, inputs),
      repo.reserve(other, inputs),
    ]);
    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(1);
    expect((await repo.read()).operations).toHaveLength(1);
  });
  it("recovers lost quote and issuance responses using the persisted preimage and request", async () => {
    const repo = repository(),
      dropped = new Set<string>(),
      issued: string[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      const url = String(input),
        response = await fetch(input, init);
      if (
        init?.method === "POST" &&
        (url.endsWith("/v1/mint/quote/bolt11") ||
          url.endsWith("/v1/mint/bolt11"))
      ) {
        if (url.endsWith("/v1/mint/bolt11")) issued.push(String(init.body));
        if (!dropped.has(url)) {
          dropped.add(url);
          throw new TypeError("Lost funding response");
        }
      }
      return response;
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    await expect(manager.createFunding(64)).rejects.toThrow("Lost");
    const saved = (await repo.read()).fundings[0];
    expect(saved.quote).toBeNull();
    expect(psPreimageHash(saved.preimage)).toBe(saved.id);
    await control("accept", { payment_hash: saved.id });
    await expect(manager.pollFunding(saved.id)).rejects.toThrow("Lost");
    expect((await repo.read()).operations).toHaveLength(1);
    const restarted = new Layer3Manager(repo, mintUrl, fetcher);
    await restarted.init();
    expect((await restarted.pollFunding(saved.id)).state).toBe("minted");
    expect((await restarted.view()).balance).toBe(64);
    expect(issued[0]).toBe(issued[1]);
  });
  it("recovers a lost full-melt receipt after its entire backing has already settled", async () => {
    const repo = repository(),
      requests: string[] = [];
    let dropped = false;
    const fetcher: typeof fetch = async (input, init) => {
      const response = await fetch(input, init);
      if (String(input).endsWith("/v1/melt/bolt11")) {
        requests.push(String(init?.body));
        if (!dropped) {
          dropped = true;
          throw new TypeError("Lost melt receipt");
        }
      }
      return response;
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 98 });
    const quote = await manager.meltQuote(request);
    await expect(manager.melt(quote)).rejects.toThrow("Lost");
    expect((await repo.read()).operations).toHaveLength(1);
    expect(
      (await wallet.pendingHTLCs()).htlcs.some(
        (h) => h.payment_hash === funding.id,
      ),
    ).toBe(false);
    const restarted = new Layer3Manager(repo, mintUrl, fetcher);
    await restarted.init();
    const op = (await repo.read()).operations[0],
      result = await restarted.retry(op.id);
    expect(result.receipt?.amount).toBe(98);
    expect(result.receipt?.change).toEqual([]);
    expect(requests[0]).toBe(requests[1]);
    expect((await restarted.view()).balance).toBe(0);
    expect((await repo.read()).operations).toEqual([]);
  });
  it("reuses the saved melt request when the same quote is submitted again", async () => {
    const repo = repository(),
      requests: string[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).endsWith("/v1/melt/bolt11")) {
        requests.push(String(init?.body));
        return new Response(
          JSON.stringify({ detail: "Payout status unknown" }),
          {
            status: 503,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      return fetch(input, init);
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const quote = await manager.meltQuote(request);
    await expect(manager.melt(quote)).rejects.toThrow("unknown");
    const anotherQuote = await manager.meltQuote(request);
    expect(anotherQuote.quote).not.toBe(quote.quote);
    await expect(manager.melt(anotherQuote)).rejects.toThrow("unknown");
    expect(requests).toHaveLength(2);
    expect(requests[0]).toBe(requests[1]);
    expect((await repo.read()).operations).toHaveLength(1);
    expect((await manager.view()).reserved).toBe(64);
    expect((await manager.view()).balance).toBe(36);
  });
  it("releases a definitively failed melt after checking the inputs are unspent", async () => {
    const repo = repository();
    let fail = true;
    const fetcher: typeof fetch = async (input, init) => {
      if (fail && String(input).endsWith("/v1/melt/bolt11"))
        return new Response(
          JSON.stringify({ detail: "melt payment failed; tokens are unspent" }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      return fetch(input, init);
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const quote = await manager.meltQuote(request);
    await expect(manager.melt(quote)).rejects.toThrow("tokens are unspent");
    expect((await repo.read()).operations).toEqual([]);
    expect((await manager.view()).balance).toBe(100);
    fail = false;
    expect((await manager.melt(quote)).amount).toBe(10);
    expect((await manager.view()).balance).toBe(88);
  });
  it("releases an expired melt quote after verifying its inputs remain unspent", async () => {
    const repo = repository(),
      manager = new Layer3Manager(repo, mintUrl);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const quote = await manager.meltQuote(request);
    await control("expire-melt-quote", { quote: quote.quote });
    await expect(manager.melt(quote)).rejects.toThrow("melt quote has expired");
    expect((await repo.read()).operations).toEqual([]);
    expect((await manager.view()).reserved).toBe(0);
    expect((await manager.view()).balance).toBe(100);
    const fresh = await manager.meltQuote(request);
    expect((await manager.melt(fresh)).amount).toBe(10);
    expect((await manager.view()).balance).toBe(88);
  });
  it("releases live inputs when another backing closes just before a combined melt", async () => {
    const repo = repository();
    let closedHash = "";
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      if (closedHash && String(input).endsWith("/v1/melt/bolt11")) {
        await control("settle", { payment_hash: closedHash });
        closedHash = "";
      }
      return fetch(input, init);
    });
    await manager.init();
    const expired = await manager.createFunding(32),
      live = await manager.createFunding(64);
    for (const funding of [expired, live]) {
      await control("accept", { payment_hash: funding.id });
      await manager.pollFunding(funding.id);
    }
    const { request } = await control("receiver", { amount: 80 });
    const quote = await manager.meltQuote(request);
    closedHash = expired.id;
    await expect(manager.melt(quote)).rejects.toThrow(/backing HTLC/);
    expect((await repo.read()).operations).toEqual([]);
    expect((await manager.view()).reserved).toBe(0);
    expect((await manager.view()).balance).toBe(64);
    expect((await manager.view()).expired).toBe(32);
  });
  it("keeps a rejected melt reserved when the proof-state check is unavailable", async () => {
    const repo = repository();
    let rejected = false;
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).endsWith("/v1/melt/bolt11")) {
        rejected = true;
        return new Response(
          JSON.stringify({ detail: "melt payment failed; tokens are unspent" }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      if (rejected && String(input).endsWith("/v1/checkstate"))
        throw new TypeError("Proof check offline");
      return fetch(input, init);
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const quote = await manager.meltQuote(request);
    await expect(manager.melt(quote)).rejects.toThrow("tokens are unspent");
    expect((await repo.read()).operations).toHaveLength(1);
    expect((await manager.view()).reserved).toBe(64);
  });
  it("keeps legacy acceptance errors reserved because their backing may become live again", async () => {
    const repo = repository();
    const manager = new Layer3Manager(repo, mintUrl, async (input, init) => {
      if (String(input).endsWith("/v1/melt/bolt11"))
        return new Response(
          JSON.stringify({
            detail:
              "backing HTLC is not fully accepted; its tokens are unavailable",
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      return fetch(input, init);
    });
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    await expect(
      manager.melt(await manager.meltQuote(request)),
    ).rejects.toThrow("not fully accepted");
    expect((await repo.read()).operations).toHaveLength(1);
    expect((await manager.view()).reserved).toBe(64);
  });
  it("preserves a payout collision request that can become eligible after another quote fails", async () => {
    const repo = repository(),
      manager = new Layer3Manager(repo, mintUrl);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const otherQuote = await wallet.createMeltQuote(request),
      other = await wallet.prepareMelt(await mint(32), otherQuote);
    await control("payment-state", {
      payment_hash: otherQuote.payment_hash,
      state: "IN_FLIGHT",
    });
    await expect(wallet.finishMelt(other)).rejects.toThrow("pending");
    const quote = await manager.meltQuote(request);
    await expect(manager.melt(quote)).rejects.toThrow(
      "payout invoice is already reserved or paid",
    );
    const saved = (await repo.read()).operations[0];
    expect(saved).toBeDefined();
    expect((await manager.view()).reserved).toBe(64);
    await control("payment-state", {
      payment_hash: quote.payment_hash,
      state: "FAILED",
    });
    await expect(wallet.finishMelt(other)).rejects.toThrow("payment failed");
    await control("payment-state", {
      payment_hash: quote.payment_hash,
      state: null,
    });
    expect((await manager.retry(saved.id)).receipt?.amount).toBe(10);
    expect((await manager.view()).balance).toBe(88);
    expect((await repo.read()).operations).toEqual([]);
  });
  it("releases unspent inputs when another quote has already paid the invoice", async () => {
    const repo = repository(),
      manager = new Layer3Manager(repo, mintUrl);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const otherQuote = await wallet.createMeltQuote(request);
    await wallet.finishMelt(
      await wallet.prepareMelt(await mint(32), otherQuote),
    );
    await expect(
      manager.melt(await manager.meltQuote(request)),
    ).rejects.toThrow("payout invoice is already paid");
    expect((await repo.read()).operations).toEqual([]);
    expect((await manager.view()).reserved).toBe(0);
    expect((await manager.view()).balance).toBe(100);
  });
  it("recovers an old duplicate melt without releasing the original reservation", async () => {
    const repo = repository();
    let dropped = false;
    const fetcher: typeof fetch = async (input, init) => {
      const response = await fetch(input, init);
      if (String(input).endsWith("/v1/melt/bolt11") && !dropped) {
        dropped = true;
        throw new TypeError("Lost melt receipt");
      }
      return response;
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    const funding = await manager.createFunding(100);
    await control("accept", { payment_hash: funding.id });
    await manager.pollFunding(funding.id);
    const { request } = await control("receiver", { amount: 10 });
    const quote = await manager.meltQuote(request);
    await expect(manager.melt(quote)).rejects.toThrow("Lost");
    const state = await repo.read(),
      original = state.operations[0],
      input = state.proofs.find(
        (p) => p.state === "ready" && p.proof.amount === 32,
      )!,
      payload = await manager.wallet.prepareMelt(
        { version: 3, mint: mintUrl, proofs: [input.proof] },
        quote,
      ),
      duplicate = {
        ...original,
        id: payload.id,
        payload,
        createdAt: original.createdAt + 1,
      };
    // Model a duplicate saved by the old wallet, before the repository guard existed.
    await repo.operations.add(duplicate);
    await repo.proofs.update(input.id, {
      state: "reserved",
      operationId: duplicate.id,
    });
    await expect(manager.retry(duplicate.id)).rejects.toThrow(
      "another request",
    );
    const remaining = await repo.read();
    expect(remaining.operations.map((op) => op.id)).toEqual([original.id]);
    expect(remaining.proofs.find((p) => p.id === input.id)?.state).toBe(
      "ready",
    );
    expect((await manager.retry(original.id)).receipt?.amount).toBe(10);
    expect((await manager.view()).balance).toBe(88);
    expect((await repo.read()).operations).toEqual([]);
  });
  it("excludes remotely pending proofs and rejects the redemption deadline boundary", async () => {
    const repo = repository();
    let pending = false;
    const fetcher: typeof fetch = async (input, init) => {
      if (pending && String(input).endsWith("/v1/checkstate"))
        return new Response(JSON.stringify({ states: ["PENDING"] }), {
          headers: { "Content-Type": "application/json" },
        });
      return fetch(input, init);
    };
    const manager = new Layer3Manager(repo, mintUrl, fetcher);
    await manager.init();
    const f = await manager.createFunding(32);
    await control("accept", { payment_hash: f.id });
    await manager.pollFunding(f.id);
    pending = true;
    await manager.refresh();
    expect((await manager.view()).balance).toBe(0);
    pending = false;
    await manager.refresh();
    expect((await manager.view()).balance).toBe(32);
    const proof = (await repo.read()).proofs.find(
      (p) => p.state === "ready",
    )!.proof;
    const snapshot = await wallet.pendingHTLCs(),
      backing = snapshot.htlcs.find((h) => h.payment_hash === f.id)!;
    expect(() =>
      checkPSBacking(proof, snapshot, backing.melt_deadline!),
    ).toThrow("deadline");
    const heightSnapshot = {
      checked_at: snapshot.checked_at,
      block_height: 90,
      htlcs: [
        {
          ...backing,
          htlc_expiry_height: 100,
          expiry_height: 98,
          blocks_remaining: 8,
          melt_deadline_height: 92,
          expires_at: null,
          melt_deadline: null,
        },
      ],
    };
    expect(checkPSBacking(proof, heightSnapshot).melt_deadline_height).toBe(92);
    expect(() =>
      checkPSBacking(proof, {
        ...heightSnapshot,
        block_height: 92,
        htlcs: [{ ...heightSnapshot.htlcs[0], blocks_remaining: 6 }],
      }),
    ).toThrow("deadline");
  });
});
