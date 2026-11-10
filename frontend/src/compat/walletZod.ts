/** Preserve the wallet SDK's one-argument record API using patched Zod 4 parsers.
 * No zod/v3 import: its collection parsers still traverse oversized inputs.
 */
import { z as modern } from "zod/v4";
export * from "zod/v4";

export function record(
  first: modern.core.$ZodType,
  second?: modern.core.$ZodType,
  params?: Parameters<typeof modern.record>[2],
) {
  return second === undefined
    ? modern.record(modern.string(), first, params)
    : modern.record(first as modern.core.$ZodRecordKey, second, params);
}

export const z = { ...modern, record };
export default z;
