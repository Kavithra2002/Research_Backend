import { connectToDatabase, disconnectFromDatabase } from "../config/db";
import { User } from "../models/User";
import { hashPassword, isHashed } from "../utils/password";

async function main() {
  await connectToDatabase();

  const users = await User.find({}, { _id: 1, user_id: 1, password: 1 });
  console.log(`Found ${users.length} user(s).`);

  let rehashed = 0;
  let skipped = 0;
  for (const user of users) {
    const current = user.password;
    if (!current) {
      console.warn(`- ${user.user_id}: empty password, skipping`);
      skipped += 1;
      continue;
    }
    if (isHashed(current)) {
      console.log(`- ${user.user_id}: already bcrypt hashed, skipping`);
      skipped += 1;
      continue;
    }
    const hashed = await hashPassword(current);
    user.password = hashed;
    await user.save();
    console.log(`+ ${user.user_id}: rehashed`);
    rehashed += 1;
  }

  console.log(`\nDone. rehashed=${rehashed} skipped=${skipped}`);
  await disconnectFromDatabase();
}

main().catch(async (err) => {
  console.error("rehash-passwords failed:", err);
  await disconnectFromDatabase().catch(() => undefined);
  process.exit(1);
});
