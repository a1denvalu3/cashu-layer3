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
    const quote = await wallet.createMeltQuote(request, 10);
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
    const quote = await wallet.createMeltQuote(request, 2);
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
    const quote = await manager.meltQuote(request, 2);
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
