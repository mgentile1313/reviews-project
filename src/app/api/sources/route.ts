import { NextResponse } from "next/server";
import { getSupabase } from "@/lib/supabase";

// GET /api/sources — list all scraped sources.
// Example route showing the Supabase access pattern.
export const dynamic = "force-dynamic";

export async function GET() {
  const { data, error } = await getSupabase()
    .from("sources")
    .select("*")
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ sources: data });
}
