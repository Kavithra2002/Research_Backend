import bcrypt from "bcryptjs";

const BCRYPT_ROUNDS = 12;
const BCRYPT_PREFIX_REGEX = /^\$2[aby]\$/;

export function isHashed(value: string): boolean {
  return BCRYPT_PREFIX_REGEX.test(value) && value.length >= 60;
}

export async function hashPassword(plain: string): Promise<string> {
  return bcrypt.hash(plain, BCRYPT_ROUNDS);
}

export async function verifyPassword(
  plain: string,
  stored: string,
): Promise<boolean> {
  if (!stored || !isHashed(stored)) return false;
  return bcrypt.compare(plain, stored);
}
