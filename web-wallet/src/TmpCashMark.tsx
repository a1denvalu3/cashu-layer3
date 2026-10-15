import type { SVGProps } from "react";

export function TmpCashMark({
  size = 24,
  strokeWidth = 1.6,
  ...props
}: SVGProps<SVGSVGElement> & { size?: number }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d="M6 3h12M6 21h12M7 3v4l10 10v4M17 3v4L7 17v4" />
      <path d="m9 18 3-3 3 3Z" fill="currentColor" stroke="none" />
    </svg>
  );
}
