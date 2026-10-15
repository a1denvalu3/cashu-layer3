import { describe, expect, it } from "vitest";
import { PSWallet, type PSSnapshot } from "@cashu/cashu-ts/tmp-cash";

const mintUrl = "https://mint.example";
const hashes = Array.from({ length: 201 }, (_, i) =>
  i.toString(16).padStart(64, "0"),
);
const empty: PSSnapshot = { checked_at: 100, block_height: null, htlcs: [] };
function backing(payment_hash: string) {
  return {
    payment_hash,
    amount: 1,
    invoice_expires_at: 100,
    htlc_expiry_height: 1018,
    expiry_height: 1000,
    blocks_remaining: 800,
    melt_deadline_height: 994,
    expires_at: null,
    melt_deadline: null,
  };
}

describe("scoped backing checks", () => {
  it("deduplicates and batches only supplied hashes, skipping an empty wallet", async () => {
    const batches: string[][] = [];
    const wallet = new PSWallet(mintUrl, async (input, init) => {
      expect(String(input)).toBe(mintUrl + "/v1/htlcs/check");
      expect(init?.method).toBe("POST");
      expect(init?.cache).toBe("no-store");
      const body = JSON.parse(String(init?.body));
      expect(Object.keys(body)).toEqual(["payment_hashes"]);
      batches.push(body.payment_hashes);
      return Response.json({ ...empty, checked_at: 100 + batches.length });
    });
    expect((await wallet.checkHTLCs([])).htlcs).toEqual([]);
    expect(batches).toEqual([]);
    expect((await wallet.checkHTLCs([...hashes, ...hashes])).checked_at).toBe(
      103,
    );
    expect(batches.map((batch) => batch.length)).toEqual([100, 100, 1]);
    expect(batches.flat()).toEqual(hashes);
  });

  it.each([
    [200, 201],
    [201, 200],
  ])("merges batch heights %i and %i conservatively", async (first, second) => {
    let calls = 0;
    const wallet = new PSWallet(mintUrl, async (_, init) => {
      const height = [first, second][calls++];
      const batch: string[] = JSON.parse(String(init?.body)).payment_hashes;
      return Response.json({
        checked_at: 100 + calls,
        block_height: height,
        htlcs: batch.map((hash) => ({
          ...backing(hash),
          blocks_remaining: 1000 - height,
        })),
      });
    });
    const result = await wallet.checkHTLCs(hashes.slice(0, 101));
    expect(result.block_height).toBe(201);
    expect(result.checked_at).toBe(102);
    expect(result.htlcs).toHaveLength(101);
    expect(new Set(result.htlcs.map((h) => h.blocks_remaining))).toEqual(
      new Set([799]),
    );
    expect(new Set(result.htlcs.map((h) => h.melt_deadline_height))).toEqual(
      new Set([994]),
    );
  });

  it("rejects an unrequested backing or duplicate result", async () => {
    for (const returned of [
      [backing(hashes[1])],
      [backing(hashes[0]), backing(hashes[0])],
    ]) {
      const wallet = new PSWallet(mintUrl, async () =>
        Response.json({ ...empty, block_height: 200, htlcs: returned }),
      );
      await expect(wallet.checkHTLCs([hashes[0]])).rejects.toThrow(
        "unexpected backing",
      );
    }
  });

  it("rejects invalid hashes before sending any request", async () => {
    let calls = 0;
    const wallet = new PSWallet(mintUrl, async () => {
      calls++;
      return Response.json(empty);
    });
    await expect(wallet.checkHTLCs([...hashes, "invalid"])).rejects.toThrow();
    expect(calls).toBe(0);
  });

  it("fails the whole check when a later batch is unavailable", async () => {
    let calls = 0;
    const wallet = new PSWallet(mintUrl, async () => {
      calls++;
      if (calls === 2)
        return Response.json({ detail: "node unavailable" }, { status: 503 });
      return Response.json(empty);
    });
    await expect(wallet.checkHTLCs(hashes)).rejects.toThrow("node unavailable");
    expect(calls).toBe(2);
  });

  it("does not accept inconsistent heights or a switch of deadline clock", async () => {
    const malformed = new PSWallet(mintUrl, async () =>
      Response.json({
        ...empty,
        block_height: 201,
        htlcs: [backing(hashes[0])],
      }),
    );
    await expect(malformed.checkHTLCs([hashes[0]])).rejects.toThrow(
      "inconsistent backing block height",
    );
    let calls = 0;
    const changing = new PSWallet(mintUrl, async () =>
      Response.json({ ...empty, block_height: calls++ === 0 ? null : 200 }),
    );
    await expect(changing.checkHTLCs(hashes)).rejects.toThrow("deadline clock");
  });
});
