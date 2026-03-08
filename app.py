#!/usr/bin/env python3
"""
Menu Analyzer - Analyzes restaurant menu photos using Claude's vision.

Usage:
    python app.py menu.jpg
    python app.py menu1.jpg menu2.jpg
    python app.py --url https://example.com/menu.jpg
"""

import argparse
import base64
import sys
from pathlib import Path
from typing import Optional

import anthropic
from pydantic import BaseModel
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()
client = anthropic.Anthropic()
MODEL = "claude-opus-4-6"


# ── Structured data models ──────────────────────────────────────────────────

class MenuItem(BaseModel):
    name: str
    description: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    category: Optional[str]
    has_photo: bool


class MenuExtraction(BaseModel):
    restaurant_name: Optional[str]
    cuisine_type: Optional[str]
    items: list[MenuItem]
    total_items_found: int
    price_range_min: Optional[float]
    price_range_max: Optional[float]
    currency: Optional[str]
    menu_language: str
    observations: str


# ── Image helpers ────────────────────────────────────────────────────────────

def load_image_base64(path: str) -> tuple[str, str]:
    """Load an image file and return (base64_data, media_type)."""
    p = Path(path)
    ext = p.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "image/jpeg")
    with open(p, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, media_type


def build_image_block(source: str) -> dict:
    """Build an image content block from a file path or URL."""
    if source.startswith("http://") or source.startswith("https://"):
        return {
            "type": "image",
            "source": {"type": "url", "url": source},
        }
    data, media_type = load_image_base64(source)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


# ── Step 1: Extract menu structure ──────────────────────────────────────────

def extract_menu(image_sources: list[str]) -> MenuExtraction:
    """Extract structured menu data from one or more images."""
    console.print("\n[bold cyan]Paso 1/2:[/] Extrayendo items y precios del menú...\n")

    content = []
    for src in image_sources:
        content.append(build_image_block(src))

    content.append({
        "type": "text",
        "text": (
            "Analyze this restaurant menu image carefully. "
            "Extract ALL menu items, their prices, descriptions, and categories. "
            "Note which items have a food photo accompanying them. "
            "If prices are not visible, set price to null. "
            "Identify the currency used. "
            "Provide observations about the menu's overall structure and quality."
        ),
    })

    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
        output_format=MenuExtraction,
    )

    return response.parsed_output


# ── Step 2: Generate recommendations ────────────────────────────────────────

def generate_recommendations(extraction: MenuExtraction, image_sources: list[str]) -> None:
    """Stream comprehensive recommendations based on the menu analysis."""
    console.print("\n[bold cyan]Paso 2/2:[/] Generando análisis y recomendaciones...\n")

    menu_summary = f"""
Restaurant: {extraction.restaurant_name or 'Unknown'}
Cuisine: {extraction.cuisine_type or 'Unknown'}
Total items: {extraction.total_items_found}
Price range: {extraction.price_range_min} - {extraction.price_range_max} {extraction.currency or ''}
Items with photos: {sum(1 for i in extraction.items if i.has_photo)}/{len(extraction.items)}
Language: {extraction.menu_language}

Menu items:
""" + "\n".join(
        f"- {item.name} | {item.category or 'N/A'} | "
        f"{item.currency or ''}{item.price or 'N/A'} | "
        f"{'📷 has photo' if item.has_photo else 'no photo'} | "
        f"Desc: {item.description or 'none'}"
        for item in extraction.items
    )

    content = []
    for src in image_sources:
        content.append(build_image_block(src))

    content.append({
        "type": "text",
        "text": f"""You are an expert restaurant consultant with deep knowledge of menu engineering, pricing strategy, and food photography.

Here is the extracted menu data:
{menu_summary}

Please provide a comprehensive analysis in Spanish with the following sections:

## 1. 💰 Análisis de Pricing Competitivo
- Evaluación de la estrategia de precios actual
- Comparativa con estándares de mercado para este tipo de cocina
- Items con precios que parecen fuera de rango (muy caros o muy baratos)
- Recomendaciones específicas de ajuste de precios

## 2. 🎯 Ideas de Promociones
- 3-5 promociones concretas adaptadas al menú actual
- Happy hours, combos, menús del día o especiales sugeridos
- Estrategias para aumentar el ticket promedio

## 3. ✍️ Mejoras en Descripciones
- Evalúa las descripciones actuales
- Identifica los items con descripciones pobres o inexistentes
- Proporciona 3-5 ejemplos de cómo reescribir descripciones para hacerlas más atractivas
- Usa técnicas de copywriting gastronómico (ingredientes premium, técnicas de cocción, origen)

## 4. 📸 Feedback sobre Fotos de los Platos
- Evalúa la calidad y uso de fotografías en el menú
- Items que definitivamente necesitan foto y por qué
- Consejos específicos de fotografía gastronómica para este menú
- Recomendaciones sobre estilo visual (ángulos, iluminación, composición)

## 5. 🏆 Resumen Ejecutivo
- Top 3 cambios prioritarios con mayor impacto
- Potencial estimado de mejora en ventas
""",
    })

    console.rule("[bold yellow]Análisis Completo del Menú")
    console.print()

    with client.messages.stream(
        model=MODEL,
        max_tokens=8192,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        thinking_shown = False
        for event in stream:
            if event.type == "content_block_start":
                if event.content_block.type == "thinking" and not thinking_shown:
                    console.print("[dim italic]Analizando en profundidad...[/]")
                    thinking_shown = True
            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    console.print(event.delta.text, end="")

    console.print("\n")


# ── Display extracted data ───────────────────────────────────────────────────

def display_extraction(extraction: MenuExtraction) -> None:
    """Show a summary table of extracted menu items."""
    console.rule("[bold green]Items Extraídos del Menú")

    info_lines = []
    if extraction.restaurant_name:
        info_lines.append(f"🍽️  Restaurante: [bold]{extraction.restaurant_name}[/]")
    if extraction.cuisine_type:
        info_lines.append(f"🌍 Cocina: [bold]{extraction.cuisine_type}[/]")
    if extraction.price_range_min and extraction.price_range_max:
        info_lines.append(
            f"💵 Rango de precios: [bold]{extraction.currency or ''}"
            f"{extraction.price_range_min} - {extraction.currency or ''}"
            f"{extraction.price_range_max}[/]"
        )
    info_lines.append(f"📋 Total de items: [bold]{extraction.total_items_found}[/]")

    for line in info_lines:
        console.print(f"  {line}")

    console.print()

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
    table.add_column("Item", style="cyan", min_width=20)
    table.add_column("Categoría", style="yellow")
    table.add_column("Precio", justify="right", style="green")
    table.add_column("Foto", justify="center")
    table.add_column("Descripción", style="dim", max_width=40)

    for item in extraction.items:
        price_str = f"{item.currency or ''}{item.price}" if item.price else "N/A"
        table.add_row(
            item.name,
            item.category or "-",
            price_str,
            "📷" if item.has_photo else "—",
            (item.description or "")[:80] + ("…" if len(item.description or "") > 80 else ""),
        )

    console.print(table)

    if extraction.observations:
        console.print(
            Panel(extraction.observations, title="[bold]Observaciones generales[/]", border_style="blue")
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analiza fotos de menús de restaurantes con IA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "images",
        nargs="+",
        metavar="IMAGE",
        help="Rutas a imágenes del menú o URLs (ej: menu.jpg o https://...)",
    )
    args = parser.parse_args()

    console.print(
        Panel.fit(
            "[bold magenta]🍽️  Menu Analyzer[/]\n"
            "[dim]Análisis inteligente de menús de restaurantes[/]",
            border_style="magenta",
        )
    )

    # Validate inputs
    for src in args.images:
        if not (src.startswith("http://") or src.startswith("https://")):
            p = Path(src)
            if not p.exists():
                console.print(f"[red]Error:[/] No se encontró el archivo: {src}")
                sys.exit(1)
            if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                console.print(f"[red]Error:[/] Formato no soportado: {src}")
                sys.exit(1)

    console.print(f"\n[dim]Analizando {len(args.images)} imagen(es)...[/]")

    # Step 1: Extract structured data
    extraction = extract_menu(args.images)
    display_extraction(extraction)

    # Step 2: Generate recommendations (streaming)
    generate_recommendations(extraction, args.images)

    console.print(
        Panel(
            "[green]✓ Análisis completado[/]\n"
            "[dim]Usa estas recomendaciones para optimizar tu menú y aumentar las ventas.[/]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()
