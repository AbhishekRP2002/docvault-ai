import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
export function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  const unit = value < 1024 ** 2 ? 1 : value < 1024 ** 3 ? 2 : 3;
  return `${(value / 1024 ** unit).toFixed(1)} ${["B", "KB", "MB", "GB"][unit]}`;
}
export function date(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(value));
}
export function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}
