import hashlib


def derive_keyset_id_ps(X2: bytes, Y_h2: bytes, Y_s2: bytes, Y_h1: bytes) -> str:
    """Identify the PS parameter set, isolated from Cashu's legacy keysets."""
    for point in (X2, Y_h2, Y_s2):
        if len(point) != 96:
            raise ValueError("PS public parameters are 96-byte G2 points")
    if len(Y_h1) != 48:
        raise ValueError("PS Y_h1 is a 48-byte G1 point")
    preimage = b"".join(
        len(p).to_bytes(4, "big") + p
        for p in (X2, Y_h2, Y_s2, Y_h1, b"ps-htlc-claim-v2")
    )
    return "03" + hashlib.sha256(preimage).hexdigest()
