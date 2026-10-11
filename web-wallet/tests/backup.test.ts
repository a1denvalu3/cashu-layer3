import { it, expect } from "vitest";
import { encryptBackup, decryptBackup } from "../src/backup";
import type { Layer3State } from "coco-cashu-core/layer3";

const empty: Layer3State = {
  proofs: [],
  operations: [],
  fundings: [],
  activity: [],
};
it("encrypts authenticated backups and rejects a wrong password or tampering", async () => {
  const password = "a test backup passphrase";
  const raw = await encryptBackup(empty, "http://localhost:3338", password);
  expect(raw).not.toContain("localhost");
  expect(raw).not.toContain("proofs");
  expect(await decryptBackup(raw, password)).toEqual({
    mint: "http://localhost:3338",
    state: empty,
  });
  await expect(
    decryptBackup(raw, "a different backup password"),
  ).rejects.toThrow();
  const envelope = JSON.parse(raw);
  envelope.ciphertext = "AAAA" + envelope.ciphertext.slice(4);
  await expect(
    decryptBackup(JSON.stringify(envelope), password),
  ).rejects.toThrow();
  await expect(
    encryptBackup(empty, "http://localhost:3338", "short"),
  ).rejects.toThrow("12");
});
