import { Card, Empty, Status } from "../../../components/ui";
import { type Product, get, initials, naira } from "../../../lib/api";

export const dynamic = "force-dynamic";

function stock(product: Product) {
  if (product.stock_qty === null) return { tone: "neutral" as const, label: "not tracked" };
  if (product.stock_qty === 0) return { tone: "critical" as const, label: "none left" };
  if (product.stock_qty <= 3) return { tone: "warning" as const, label: `${product.stock_qty} left` };
  return { tone: "good" as const, label: `${product.stock_qty} in stock` };
}

export default async function Catalogue() {
  const { products } = await get<{ products: Product[] }>(`/catalogue`);
  const live = products.filter((p) => p.active).length;
  const out = products.filter((p) => p.stock_qty === 0).length;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Catalogue</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {products.length === 0
            ? "nothing yet"
            : `${live} active${out > 0 ? ` · ${out} out of stock` : ""}`}
        </span>
      </header>

      {products.length === 0 ? (
        <Card><Empty>No products. Seed the catalogue with <span className="num">python -m aisales_worker --seed</span>.</Empty></Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {products.map((p) => {
            const state = stock(p);
            return (
              <Card key={p.id} bodyClassName="p-4">
                <div className="flex gap-3">
                  <span
                    className="flex items-center justify-center text-[14px] font-semibold"
                    style={{
                      width: 58, height: 58, flex: "none",
                      borderRadius: "var(--radius-md)",
                      backgroundImage:
                        "repeating-linear-gradient(45deg, rgba(255,255,255,0.06) 0 3px, transparent 3px 6px)," +
                        "repeating-linear-gradient(-45deg, rgba(0,0,0,0.10) 0 3px, transparent 3px 6px)," +
                        `linear-gradient(160deg, var(--series-2), var(--series-1))`,
                      opacity: p.active ? 1 : 0.5,
                    }}
                    aria-hidden
                  >
                    {initials(p.name)}
                  </span>
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium leading-snug">{p.name}</div>
                    <div className="num text-[11px]" style={{ color: "var(--faint)" }}>{p.sku}</div>
                    <div className="num mt-1 text-[14px] font-semibold">{naira(p.price_kobo)}</div>
                  </div>
                </div>
                <p className="mt-2.5 line-clamp-2 text-[12px]" style={{ color: "var(--muted)" }}>
                  {p.description}
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <Status tone={state.tone}>{state.label}</Status>
                  {p.sold > 0 && <span className="chip num">{p.sold} sold</span>}
                  {!p.active && <Status>hidden</Status>}
                </div>
                {p.variants.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {p.variants.slice(0, 4).map((v, i) => (
                      <span key={i} className="chip">{Object.values(v).join(" · ")}</span>
                    ))}
                    {p.variants.length > 4 && <span className="chip">+{p.variants.length - 4}</span>}
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
