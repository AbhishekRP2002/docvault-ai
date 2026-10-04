import { describe, expect, test } from "bun:test";
import {
  chartCeiling,
  chartIndex,
  chartSegments,
  formatCost,
  usageDate,
  usageValue,
  type RecordedUsage,
} from "./usage";

const usage: RecordedUsage = {
  requests: 2,
  input_tokens: 40,
  output_tokens: 10,
  cost_usd: null,
  unknown_cost_calls: 2,
};

describe("usage chart accounting", () => {
  test("keeps cost unavailable distinct from known zero and tiny positive costs", () => {
    expect(usageValue(usage, "tokens")).toBe(50);
    expect(usageValue(usage, "cost")).toBeNull();
    expect(formatCost(null)).toBe("Unavailable");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(0.000001)).toBe("<$0.0001");
    expect(formatCost(0.0023)).toBe("$0.0023");
  });

  test("draws gaps for unknown cost while retaining real zero days", () => {
    const segments = chartSegments([0, 2, null, 4, null], 4);
    expect(segments).toEqual([
      [
        { x: 56, y: 234 },
        { x: 256, y: 131 },
      ],
      [{ x: 656, y: 28 }],
    ]);
    expect(chartSegments([null, null], 1)).toEqual([]);
  });

  test("keeps empty and single-point charts finite", () => {
    expect(chartCeiling([0, null], "requests")).toBe(4);
    expect(chartCeiling([null], "cost")).toBe(0.01);
    expect(chartSegments([2], 4)).toEqual([[{ x: 56, y: 131 }]]);
    expect(chartCeiling([50], "tokens")).toBeGreaterThan(50);
  });

  test("clamps pointer selection to actual days at responsive chart widths", () => {
    expect(chartIndex(-20, 880, 7)).toBe(0);
    expect(chartIndex(456, 880, 7)).toBe(3);
    expect(chartIndex(456 / 2, 440, 7)).toBe(3);
    expect(chartIndex(1000, 880, 7)).toBe(6);
  });

  test("formats day buckets in UTC regardless of browser timezone", () => {
    expect(usageDate("2026-10-05")).toBe("Oct 5");
    expect(usageDate("2026-10-05", true)).toBe("Oct 5, 2026");
  });
});
