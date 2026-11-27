/**
 * Minimal strict JSON-schema validator for WebMCP tool input.
 *
 * Supports the subset the tool schemas use: object with `properties`,
 * `required`, `additionalProperties: false`; string (`maxLength`, `pattern`,
 * `enum`), integer (`minimum`, `maximum`). Anything unknown is rejected, so a
 * malicious caller cannot smuggle extra fields through to a service call.
 *
 * @module integrations/webmcp/schema
 */

export type JsonSchema =
  | {
      type: "string";
      description?: string;
      maxLength?: number;
      minLength?: number;
      pattern?: string;
      enum?: readonly string[];
    }
  | { type: "integer"; description?: string; minimum?: number; maximum?: number };

export interface ObjectSchema {
  type: "object";
  description?: string;
  properties: Record<string, JsonSchema>;
  required?: readonly string[];
  additionalProperties: false;
}

export type ValidationResult =
  { ok: true; value: Record<string, unknown> } | { ok: false; message: string };

export function validateInput(schema: ObjectSchema, input: unknown): ValidationResult {
  const candidate = input === undefined || input === null ? {} : input;
  if (typeof candidate !== "object" || Array.isArray(candidate)) {
    return { ok: false, message: "Input must be an object." };
  }
  const record = candidate as Record<string, unknown>;
  const proto = Object.getPrototypeOf(record);
  if (proto !== Object.prototype && proto !== null) {
    return { ok: false, message: "Input must be a plain object." };
  }
  for (const key of Object.keys(record)) {
    if (!Object.prototype.hasOwnProperty.call(schema.properties, key)) {
      return { ok: false, message: `Unknown input field: ${key.slice(0, 40)}` };
    }
  }
  for (const key of schema.required ?? []) {
    if (record[key] === undefined) return { ok: false, message: `Missing required field: ${key}` };
  }
  const value: Record<string, unknown> = {};
  for (const [key, rule] of Object.entries(schema.properties)) {
    const field = record[key];
    if (field === undefined) continue;
    if (rule.type === "string") {
      if (typeof field !== "string") return { ok: false, message: `${key} must be a string.` };
      if (rule.maxLength !== undefined && field.length > rule.maxLength) {
        return { ok: false, message: `${key} exceeds ${rule.maxLength} characters.` };
      }
      if (rule.minLength !== undefined && field.length < rule.minLength) {
        return { ok: false, message: `${key} is shorter than ${rule.minLength} characters.` };
      }
      if (rule.pattern && !new RegExp(rule.pattern).test(field)) {
        return { ok: false, message: `${key} has an invalid format.` };
      }
      if (rule.enum && !rule.enum.includes(field)) {
        return { ok: false, message: `${key} must be one of: ${rule.enum.join(", ")}.` };
      }
      value[key] = field;
    } else {
      if (typeof field !== "number" || !Number.isInteger(field)) {
        return { ok: false, message: `${key} must be an integer.` };
      }
      if (rule.minimum !== undefined && field < rule.minimum) {
        return { ok: false, message: `${key} must be at least ${rule.minimum}.` };
      }
      if (rule.maximum !== undefined && field > rule.maximum) {
        return { ok: false, message: `${key} must be at most ${rule.maximum}.` };
      }
      value[key] = field;
    }
  }
  return { ok: true, value };
}
