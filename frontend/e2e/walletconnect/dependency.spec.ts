import { expect, test } from "@playwright/test";

// Actual published SDK schemas and actual patched parsers; no provider mock.
test("Coinbase EIP-712 records and AppKit frame schemas preserve their validation contract", async ({
  page,
}) => {
  await page.goto("/login");
  const result = await page.evaluate(async () => {
    const sdkPath = "/node_modules/@coinbase/cdp-sdk/_esm/policies/evmSchema.js";
    const framePath = "/node_modules/@reown/appkit-wallet/dist/esm/src/W3mFrameSchema.js";
    const { SignEvmTypedDataFieldCriterionSchema: schema } = await import(
      /* @vite-ignore */ sdkPath
    );
    const { AppSyncThemeRequest, AppConnectEmailRequest } = await import(
      /* @vite-ignore */ framePath
    );
    const value = {
      type: "evmTypedDataField",
      types: { types: { Message: [{ name: "amount", type: "uint256" }] }, primaryType: "Message" },
      conditions: [{ value: "100", operator: "<=", path: "amount" }],
    };
    return {
      valid: schema.safeParse(value).success,
      invalid: schema.safeParse({
        ...value,
        types: { ...value.types, types: { Message: [{ name: "amount", type: 1 }] } },
      }).success,
      email: AppConnectEmailRequest.safeParse({ email: "alice@example.com" }).success,
      badEmail: AppConnectEmailRequest.safeParse({ email: "invalid" }).success,
      theme: AppSyncThemeRequest.safeParse({
        themeMode: "dark",
        themeVariables: { "--w3m-accent": "#123456" },
      }).success,
    };
  });
  expect(result).toEqual({
    valid: true,
    invalid: false,
    email: true,
    badEmail: false,
    theme: true,
  });
});

test("oversized constrained arrays and sets abort before parsing elements", async ({ page }) => {
  await page.goto("/login");
  const result = await page.evaluate(async () => {
    const path = "/src/compat/walletZod.ts";
    const { z } = await import(/* @vite-ignore */ path);
    let visited = 0;
    const item = z.string().superRefine(() => {
      visited++;
    });
    const array = z
      .array(item)
      .max(1)
      .safeParse(Array.from({ length: 10000 }, () => "x"));
    const set = z
      .set(item)
      .max(1)
      .safeParse(new Set(Array.from({ length: 10000 }, (_, n) => String(n))));
    const asyncArray = await z.array(item).max(1).safeParseAsync(["a", "b"]);
    return { array: array.success, set: set.success, asyncArray: asyncArray.success, visited };
  });
  expect(result).toEqual({ array: false, set: false, asyncArray: false, visited: 0 });
});
