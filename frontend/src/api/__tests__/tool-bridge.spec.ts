import { describe, expect, it, vi } from "vitest";

const requestMock = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({
  apiRequest: requestMock,
  toQuery: (params: Record<string, string | number | boolean | null | undefined>) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
    }
    const rendered = query.toString();
    return rendered ? `?${rendered}` : "";
  },
}));

import { toolBridgeApi } from "@/api/tool-bridge";

describe("toolBridgeApi", () => {
  it("uses the Tool Bridge calls collection endpoint", async () => {
    requestMock.mockResolvedValue({ items: [], total: 0 });
    await toolBridgeApi.list({ status: "DENIED", toolName: "plugin.echo.text", limit: 20, offset: 40 });
    expect(requestMock).toHaveBeenCalledWith(
      "/tool-bridge/calls?status=DENIED&tool_name=plugin.echo.text&limit=20&offset=40",
    );
  });

  it("encodes the call id on the detail endpoint", async () => {
    requestMock.mockResolvedValue({ id: "call" });
    await toolBridgeApi.get("call/id");
    expect(requestMock).toHaveBeenCalledWith("/tool-bridge/calls/call%2Fid");
  });
});
