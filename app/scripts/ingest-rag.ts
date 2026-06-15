import { connectToDatabase, disconnectFromDatabase } from "../config/db";
import { reindexAll, resolveCompanySlug } from "../services/rag/ingest";

/**
 * Build (or refresh) the RAG vector index in MongoDB.
 *
 *   npm run rag:ingest                 # index every company / source
 *   npm run rag:ingest -- --company "Colombo Land"   # one company only
 */
async function main() {
  const args = process.argv.slice(2);
  let company: string | undefined;
  const idx = args.indexOf("--company");
  if (idx !== -1 && args[idx + 1]) company = args[idx + 1];

  await connectToDatabase();

  let companySlug: string | undefined;
  if (company) {
    companySlug = (await resolveCompanySlug(company)) ?? undefined;
    console.log(`Scoping to company "${company}" → slug "${companySlug}"`);
  }

  console.log("Indexing report text into MongoDB (document_chunks)…");
  const result = await reindexAll(companySlug);

  console.log("\nDone.");
  console.log(
    `  financial_table: +${result.financial_table.inserted} inserted, ` +
      `${result.financial_table.skipped} unchanged, ` +
      `${result.financial_table.deleted} pruned`,
  );
  console.log(
    `  non_financial:   +${result.non_financial.inserted} inserted, ` +
      `${result.non_financial.skipped} unchanged, ` +
      `${result.non_financial.deleted} pruned`,
  );

  await disconnectFromDatabase();
}

main().catch(async (err) => {
  console.error("rag:ingest failed:", err);
  await disconnectFromDatabase().catch(() => undefined);
  process.exit(1);
});
