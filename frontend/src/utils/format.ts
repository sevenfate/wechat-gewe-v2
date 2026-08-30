export function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatInteger(value?: number | null): string {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatCurrency(amount?: number | null, currency?: string | null): string {
  if (amount === null || amount === undefined) return "-";
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: currency || "CNY",
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency || "CNY"}`;
  }
}
