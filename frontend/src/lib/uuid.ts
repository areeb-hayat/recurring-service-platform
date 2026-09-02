/**
 * UUIDv7 generation.
 *
 * P0 §7.2 specifies a v7 operation id generated on the device. v7 is
 * time-ordered, so an outbox sorted by id is in the order the user acted — which
 * matters to P5, and is free to get right now. `crypto.randomUUID()` is v4 and
 * would lose that ordering, so it is not used for operation ids.
 *
 * No dependency: v7 is 48 bits of Unix milliseconds, 4 version bits, 12 random
 * bits, 2 variant bits and 62 more random bits.
 */

const HEX = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, "0"));

export function uuidv7(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);

  const millis = BigInt(Date.now());
  for (let i = 5; i >= 0; i -= 1) {
    bytes[i] = Number((millis >> BigInt((5 - i) * 8)) & 0xffn);
  }
  bytes[6] = (bytes[6]! & 0x0f) | 0x70; // version 7
  bytes[8] = (bytes[8]! & 0x3f) | 0x80; // RFC 4122 variant

  const h = (i: number) => HEX[bytes[i]!]!;
  return (
    h(0) + h(1) + h(2) + h(3) + "-" +
    h(4) + h(5) + "-" +
    h(6) + h(7) + "-" +
    h(8) + h(9) + "-" +
    h(10) + h(11) + h(12) + h(13) + h(14) + h(15)
  );
}
