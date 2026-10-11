import type { MintQuoteBolt11Response } from '@cashu/cashu-ts';

export interface MintQuote extends MintQuoteBolt11Response {
  mintUrl: string;
  method: 'bolt11' | 'bolt12';
}
