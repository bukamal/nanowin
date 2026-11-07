from __future__ import annotations

import json
from datetime import date, timedelta

from nano_offline.core import invoice_settings
from nano_offline.services.accounting_rebuilder import OPENING_BALANCE_MARKER
from nano_offline.core.asyncdb import Offloadable, blocking
from nano_offline.core.database import Database

EPSILON = 1e-9


class ReportingService(Offloadable):
    """Read-only local reports derived from source documents and rebuilt ledger state."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _normalize_dates(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
        start = (date_from or "").strip() or None
        end = (date_to or "").strip() or None
        for value in (start, end):
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("التاريخ يجب أن يكون بصيغة YYYY-MM-DD") from exc
        if start and end and start > end:
            raise ValueError("تاريخ البداية يجب ألا يكون بعد تاريخ النهاية")
        return start, end

    @staticmethod
    def _date_clause(column: str, date_from: str | None, date_to: str | None) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if date_from:
            clauses.append(f"{column}>=?")
            params.append(date_from)
        if date_to:
            clauses.append(f"{column}<=?")
            params.append(date_to)
        return (" AND ".join(clauses) if clauses else "1=1"), params

    @blocking
    def income_statement(self, *, date_from: str | None = None, date_to: str | None = None) -> dict:
        date_from, date_to = self._normalize_dates(date_from, date_to)
        clause, params = self._date_clause("i.invoice_date", date_from, date_to)
        opening_clause, opening_params = self._date_clause("i.invoice_date", None, date_to)
        e_clause, e_params = self._date_clause("e.expense_date", date_from, date_to)
        with self.db.connect() as conn:
            sales = float(conn.execute(
                f"SELECT COALESCE(SUM(i.total),0) FROM invoices i WHERE i.type='sale' AND {clause}", params
            ).fetchone()[0])
            opening_inventory = float(conn.execute(
                f"SELECT COALESCE(SUM(i.total),0) FROM invoices i WHERE i.type='purchase' "
                f"AND i.notes LIKE ? AND {opening_clause}",
                [f"%{OPENING_BALANCE_MARKER}%", *opening_params],
            ).fetchone()[0])
            purchases = float(conn.execute(
                f"SELECT COALESCE(SUM(i.total),0) FROM invoices i WHERE i.type='purchase' "
                f"AND COALESCE(i.notes,'') NOT LIKE ? AND {clause}",
                [f"%{OPENING_BALANCE_MARKER}%", *params],
            ).fetchone()[0])
            cogs = float(conn.execute(
                f"""SELECT COALESCE(SUM(il.cost_amount),0)
                    FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id
                    WHERE i.type='sale' AND {clause}""", params
            ).fetchone()[0])
            expenses = float(conn.execute(
                f"SELECT COALESCE(SUM(e.amount),0) FROM expenses e WHERE {e_clause}", e_params
            ).fetchone()[0])
            expense_rows = [dict(r) for r in conn.execute(
                f"""SELECT COALESCE(ec.name,e.category,'بلا تصنيف') AS category,
                           SUM(e.amount) AS amount, COUNT(*) AS count
                    FROM expenses e LEFT JOIN expense_categories ec ON ec.id=e.category_id
                    WHERE {e_clause}
                    GROUP BY COALESCE(ec.name,e.category,'بلا تصنيف')
                    ORDER BY amount DESC,category""", e_params
            ).fetchall()]
            gross_profit = sales - cogs
            net_profit = gross_profit - expenses
            return {
                "date_from": date_from,
                "date_to": date_to,
                "sales": sales,
                "purchases": purchases,
                "opening_inventory": opening_inventory,
                "cogs": cogs,
                "gross_profit": gross_profit,
                "gross_margin_percent": (gross_profit / sales * 100.0) if sales > EPSILON else 0.0,
                "expenses": expenses,
                "net_profit": net_profit,
                "net_margin_percent": (net_profit / sales * 100.0) if sales > EPSILON else 0.0,
                "markup_percent": (gross_profit / cogs * 100.0) if cogs > EPSILON else 0.0,
                "expense_breakdown": expense_rows,
            }

    @blocking
    def profit_trend(self, *, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """Monthly sales, COGS, expenses and profit, excluding opening stock."""
        date_from, date_to = self._normalize_dates(date_from, date_to)
        invoice_clause, invoice_params = self._date_clause("i.invoice_date", date_from, date_to)
        expense_clause, expense_params = self._date_clause("e.expense_date", date_from, date_to)
        with self.db.connect() as conn:
            sales_rows = conn.execute(
                f"""SELECT substr(i.invoice_date,1,7) AS month,
                           SUM(CASE WHEN i.type='sale' THEN i.total ELSE 0 END) AS sales,
                           SUM(CASE WHEN i.type='sale' THEN
                               (SELECT COALESCE(SUM(il.cost_amount),0) FROM invoice_lines il WHERE il.invoice_id=i.id)
                               ELSE 0 END) AS cogs
                    FROM invoices i
                    WHERE {invoice_clause}
                    GROUP BY substr(i.invoice_date,1,7)""",
                invoice_params,
            ).fetchall()
            expense_rows = conn.execute(
                f"""SELECT substr(e.expense_date,1,7) AS month, SUM(e.amount) AS expenses
                    FROM expenses e WHERE {expense_clause}
                    GROUP BY substr(e.expense_date,1,7)""",
                expense_params,
            ).fetchall()
        totals: dict[str, dict[str, float]] = {}
        for row in sales_rows:
            totals[str(row["month"])] = {
                "sales": float(row["sales"] or 0),
                "cogs": float(row["cogs"] or 0),
                "expenses": 0.0,
            }
        for row in expense_rows:
            totals.setdefault(str(row["month"]), {"sales": 0.0, "cogs": 0.0, "expenses": 0.0})
            totals[str(row["month"])]["expenses"] = float(row["expenses"] or 0)
        result = []
        for month, values in sorted(totals.items()):
            gross = values["sales"] - values["cogs"]
            result.append({
                "month": month,
                **values,
                "gross_profit": gross,
                "net_profit": gross - values["expenses"],
            })
        return result

    @blocking
    def period_comparison(self, *, date_from: str, date_to: str) -> dict:
        """Compare a bounded period with the immediately preceding equal-length period."""
        start, end = self._normalize_dates(date_from, date_to)
        if start is None or end is None:
            raise ValueError("للمقارنة حدد تاريخ بداية ونهاية")
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        days = (end_date - start_date).days + 1
        previous_end = start_date - timedelta(days=1)
        previous_start = previous_end - timedelta(days=days - 1)
        current = self.income_statement(date_from=start, date_to=end)
        previous = self.income_statement(
            date_from=previous_start.isoformat(), date_to=previous_end.isoformat()
        )
        metrics = ("sales", "gross_profit", "gross_margin_percent", "expenses", "net_profit", "net_margin_percent")
        changes = {}
        for key in metrics:
            before, now = float(previous[key]), float(current[key])
            changes[key] = {
                "previous": before,
                "current": now,
                "difference": now - before,
                "change_percent": ((now - before) / abs(before) * 100.0) if abs(before) > EPSILON else None,
            }
        return {
            "date_from": start,
            "date_to": end,
            "previous_from": previous_start.isoformat(),
            "previous_to": previous_end.isoformat(),
            "metrics": changes,
        }

    @blocking
    def invoice_profitability(self, *, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        date_from, date_to = self._normalize_dates(date_from, date_to)
        clause, params = self._date_clause("i.invoice_date", date_from, date_to)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT i.id,i.invoice_date,i.reference,i.total,i.paid_amount,
                           COALESCE(c.name,'نقدي') AS customer_name,
                           COALESCE(SUM(il.cost_amount),0) AS cogs
                    FROM invoices i
                    LEFT JOIN customers c ON c.id=i.customer_id
                    LEFT JOIN invoice_lines il ON il.invoice_id=i.id
                    WHERE i.type='sale' AND {clause}
                    GROUP BY i.id
                    ORDER BY i.invoice_date DESC,i.id DESC""",
                params,
            ).fetchall()
            result: list[dict] = []
            for raw in rows:
                row = dict(raw)
                total = float(row["total"] or 0)
                cogs = float(row["cogs"] or 0)
                profit = total - cogs
                row["gross_profit"] = profit
                row["margin_percent"] = (profit / total * 100.0) if total > EPSILON else 0.0
                row["markup_percent"] = (profit / cogs * 100.0) if cogs > EPSILON else 0.0
                row["remaining_amount"] = max(0.0, total - float(row["paid_amount"] or 0))
                result.append(row)
            return result

    @blocking
    def item_profitability(self, *, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        date_from, date_to = self._normalize_dates(date_from, date_to)
        clause, params = self._date_clause("i.invoice_date", date_from, date_to)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT COALESCE(it.id,0) AS item_id,
                           COALESCE(it.name,il.description) AS item_name,
                           COALESCE(it.item_type,'خدمة') AS item_type,
                           COALESCE(u.name,'') AS base_unit_name,
                           SUM(il.quantity_in_base) AS quantity_in_base,
                           SUM(il.total) AS revenue,
                           SUM(il.cost_amount) AS cogs,
                           COUNT(DISTINCT i.id) AS invoice_count
                    FROM invoice_lines il
                    JOIN invoices i ON i.id=il.invoice_id
                    LEFT JOIN items it ON it.id=il.item_id
                    LEFT JOIN units u ON u.id=it.base_unit_id
                    WHERE i.type='sale' AND {clause}
                    GROUP BY COALESCE(it.id,0),COALESCE(it.name,il.description),COALESCE(it.item_type,'خدمة'),COALESCE(u.name,'')
                    ORDER BY revenue DESC,item_name""",
                params,
            ).fetchall()
            result: list[dict] = []
            for raw in rows:
                row = dict(raw)
                revenue = float(row["revenue"] or 0)
                cogs = float(row["cogs"] or 0)
                profit = revenue - cogs
                row["gross_profit"] = profit
                row["margin_percent"] = (profit / revenue * 100.0) if revenue > EPSILON else 0.0
                row["markup_percent"] = (profit / cogs * 100.0) if cogs > EPSILON else 0.0
                result.append(row)
            return result

    @blocking
    def top_selling_items(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        order_by: str = "revenue",
    ) -> list[dict]:
        rows = self.item_profitability(date_from=date_from, date_to=date_to)
        key = "quantity_in_base" if order_by == "quantity" else "revenue"
        rows.sort(key=lambda row: float(row.get(key) or 0), reverse=True)
        return rows[: max(1, int(limit))]

    @blocking
    def inventory_report(self, *, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """Inventory movement and valuation by item for a period.

        Opening inventory is a timeless baseline. Historical value is deterministic
        because inventory_movements.value_delta stores purchase value and sale COGS.
        """
        date_from, date_to = self._normalize_dates(date_from, date_to)
        with self.db.connect() as conn:
            items = conn.execute(
                """SELECT i.id,i.name,i.opening_quantity,i.opening_unit_cost,i.quantity,i.average_cost,
                          COALESCE(u.name,'') AS unit_name
                   FROM items i LEFT JOIN units u ON u.id=i.base_unit_id
                   WHERE i.item_type='مخزون' ORDER BY i.name"""
            ).fetchall()
            result: list[dict] = []
            for raw in items:
                item = dict(raw)
                iid = int(item["id"])
                opening_qty = float(item["opening_quantity"] or 0)
                opening_value = opening_qty * float(item["opening_unit_cost"] or 0)
                if date_from:
                    before = conn.execute(
                        """SELECT COALESCE(SUM(quantity_delta),0),COALESCE(SUM(value_delta),0)
                           FROM inventory_movements
                           WHERE item_id=? AND invoice_id IS NOT NULL AND movement_date<?""",
                        (iid, date_from),
                    ).fetchone()
                    opening_qty += float(before[0] or 0)
                    opening_value += float(before[1] or 0)

                period_where = ["item_id=?", "invoice_id IS NOT NULL"]
                p: list[object] = [iid]
                if date_from:
                    period_where.append("movement_date>=?")
                    p.append(date_from)
                if date_to:
                    period_where.append("movement_date<=?")
                    p.append(date_to)
                stats = conn.execute(
                    f"""SELECT
                            COALESCE(SUM(CASE WHEN quantity_delta>0 THEN quantity_delta ELSE 0 END),0) AS in_qty,
                            COALESCE(SUM(CASE WHEN quantity_delta<0 THEN -quantity_delta ELSE 0 END),0) AS out_qty,
                            COALESCE(SUM(CASE WHEN value_delta>0 THEN value_delta ELSE 0 END),0) AS in_value,
                            COALESCE(SUM(CASE WHEN value_delta<0 THEN -value_delta ELSE 0 END),0) AS out_value,
                            COALESCE(SUM(quantity_delta),0) AS net_qty,
                            COALESCE(SUM(value_delta),0) AS net_value
                        FROM inventory_movements WHERE {' AND '.join(period_where)}""",
                    p,
                ).fetchone()
                closing_qty = opening_qty + float(stats["net_qty"] or 0)
                closing_value = opening_value + float(stats["net_value"] or 0)
                if abs(closing_qty) <= EPSILON:
                    closing_qty = 0.0
                    closing_value = 0.0
                result.append({
                    **item,
                    "opening_quantity_period": opening_qty,
                    "opening_value": opening_value,
                    "purchases_quantity": float(stats["in_qty"] or 0),
                    "sales_quantity": float(stats["out_qty"] or 0),
                    "purchases_value": float(stats["in_value"] or 0),
                    "cogs_value": float(stats["out_value"] or 0),
                    "closing_quantity": closing_qty,
                    "closing_value": closing_value,
                    "closing_unit_cost": (closing_value / closing_qty) if closing_qty > EPSILON else 0.0,
                })
            return result

    @blocking
    def inventory_valuation(self, *, as_of: str | None = None) -> dict:
        _, as_of = self._normalize_dates(None, as_of)
        rows = self.inventory_report(date_to=as_of)
        return {
            "as_of": as_of,
            "rows": rows,
            "total_quantity": sum(float(r["closing_quantity"]) for r in rows),
            "total_value": sum(float(r["closing_value"]) for r in rows),
            "item_count": len(rows),
        }

    @blocking
    def party_balances(self, party_type: str, *, as_of: str | None = None) -> dict:
        _, as_of = self._normalize_dates(None, as_of)
        if party_type == "customer":
            table, account, sign, invoice_type, column = "customers", "AR", 1, "sale", "customer_id"
        elif party_type == "supplier":
            table, account, sign, invoice_type, column = "suppliers", "AP", -1, "purchase", "supplier_id"
        else:
            raise ValueError("نوع الحساب غير صحيح")
        with self.db.connect() as conn:
            parties = conn.execute(f"SELECT id,name,phone,address FROM {table} ORDER BY name").fetchall()
            result: list[dict] = []
            positive = 0.0
            credits = 0.0
            for p in parties:
                ledger_sql = "SELECT COALESCE(SUM(debit-credit),0) FROM ledger_entries WHERE party_type=? AND party_id=? AND account_code=?"
                params: list[object] = [party_type, p["id"], account]
                if as_of:
                    ledger_sql += " AND entry_date<=?"
                    params.append(as_of)
                raw_balance = float(conn.execute(ledger_sql, params).fetchone()[0])
                balance = raw_balance if sign == 1 else -raw_balance

                invoice_sql = f"SELECT COUNT(*) FROM invoices WHERE type=? AND {column}=?"
                ip: list[object] = [invoice_type, p["id"]]
                if as_of:
                    invoice_sql += " AND invoice_date<=?"
                    ip.append(as_of)
                invoice_count = int(conn.execute(invoice_sql, ip).fetchone()[0])

                if balance > EPSILON:
                    positive += balance
                elif balance < -EPSILON:
                    credits += -balance
                result.append({**dict(p), "balance": balance, "invoice_count": invoice_count})
            result.sort(key=lambda r: abs(float(r["balance"])), reverse=True)
            return {
                "party_type": party_type,
                "as_of": as_of,
                "rows": result,
                "positive_total": positive,
                "credit_total": credits,
                "net_total": positive - credits,
            }

    @blocking
    def outstanding_invoices(self, party_type: str, *, as_of: str | None = None) -> list[dict]:
        _, as_of = self._normalize_dates(None, as_of)
        if party_type == "customer":
            invoice_type, column, party_table = "sale", "customer_id", "customers"
        elif party_type == "supplier":
            invoice_type, column, party_table = "purchase", "supplier_id", "suppliers"
        else:
            raise ValueError("نوع الحساب غير صحيح")
        with self.db.connect() as conn:
            sql = f"""SELECT i.id,i.invoice_date,i.reference,i.total,i.print_options,i.{column} AS party_id,p.name AS party_name,
                              COALESCE((SELECT SUM(pa.amount)
                                        FROM payment_allocations pa
                                        JOIN payments pay ON pay.id=pa.payment_id
                                        WHERE pa.invoice_id=i.id {{pay_date_filter}}),0) AS paid_as_of
                       FROM invoices i
                       JOIN {party_table} p ON p.id=i.{column}
                       WHERE i.type=? {{invoice_date_filter}}
                       ORDER BY i.invoice_date,i.id"""
            pay_filter = ""
            invoice_filter = ""
            params: list[object] = []
            if as_of:
                pay_filter = "AND pay.payment_date<=?"
                invoice_filter = "AND i.invoice_date<=?"
                params = [as_of, invoice_type, as_of]
            else:
                params = [invoice_type]
            rows = conn.execute(sql.format(pay_date_filter=pay_filter, invoice_date_filter=invoice_filter), params).fetchall()
            result = []
            aged_as_of = date.fromisoformat(as_of or date.today().isoformat())
            for raw in rows:
                row = dict(raw)
                remaining = float(row["total"] or 0) - float(row["paid_as_of"] or 0)
                if remaining > EPSILON:
                    row["remaining_amount"] = remaining
                    row["age_days"] = max(
                        0,
                        (aged_as_of - date.fromisoformat(str(row["invoice_date"])[:10])).days,
                    )
                    row["age_bucket"] = (
                        "0-30" if row["age_days"] <= 30 else
                        "31-60" if row["age_days"] <= 60 else
                        "61-90" if row["age_days"] <= 90 else "90+"
                    )
                    result.append(row)
            return result

    @blocking
    def aging_summary(self, party_type: str, *, as_of: str | None = None) -> dict:
        """Outstanding invoice aging by invoice age (not contractual due date)."""
        rows = self.outstanding_invoices(party_type, as_of=as_of)
        buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
        parties: dict[int, dict] = {}
        for row in rows:
            bucket = str(row["age_bucket"])
            amount = float(row["remaining_amount"])
            buckets[bucket] += amount
            party_id = int(row["party_id"])
            party = parties.setdefault(party_id, {
                "party_id": party_id,
                "party_name": row["party_name"],
                "balance": 0.0,
                "oldest_days": 0,
            })
            party["balance"] += amount
            party["oldest_days"] = max(party["oldest_days"], int(row["age_days"]))
        return {
            "party_type": party_type,
            "as_of": as_of or date.today().isoformat(),
            "basis": "invoice_date",
            "buckets": buckets,
            "total": sum(buckets.values()),
            "parties": sorted(parties.values(), key=lambda row: row["balance"], reverse=True),
            "invoices": rows,
        }

    @blocking
    def cash_outlook(self, *, as_of: str | None = None, lookback_days: int = 30) -> dict:
        """Scenario projection from recent average daily cash movement, not scheduled dues."""
        end = date.fromisoformat(as_of) if as_of else date.today()
        lookback_days = max(7, min(int(lookback_days), 365))
        start = end - timedelta(days=lookback_days - 1)
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(SUM(debit),0) AS receipts, COALESCE(SUM(credit),0) AS payments
                   FROM ledger_entries WHERE account_code='CASH' AND entry_date>=? AND entry_date<=?""",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
            balance = float(conn.execute(
                "SELECT COALESCE(SUM(debit-credit),0) FROM ledger_entries "
                "WHERE account_code='CASH' AND entry_date<=?",
                (end.isoformat(),),
            ).fetchone()[0])
        receipts = float(rows["receipts"] or 0)
        payments = float(rows["payments"] or 0)
        daily_net = (receipts - payments) / lookback_days
        return {
            "as_of": end.isoformat(),
            "lookback_days": lookback_days,
            "current_balance": balance,
            "average_daily_receipts": receipts / lookback_days,
            "average_daily_payments": payments / lookback_days,
            "average_daily_net": daily_net,
            "scenario_only": True,
            "projections": [
                {"days": days, "balance": balance + daily_net * days}
                for days in (7, 30, 90)
            ],
        }

    @blocking
    def collection_priorities(self, *, as_of: str | None = None) -> dict:
        """Rank unpaid customer invoices by actual configured due date.

        Legacy invoices without a due-date snapshot use the current default
        due-days setting. If no due-date policy is configured, age is shown
        but the invoice is not represented as contractually overdue.
        """
        end = date.fromisoformat(as_of) if as_of else date.today()
        rows = self.outstanding_invoices("customer", as_of=end.isoformat())
        with self.db.connect() as conn:
            setting = conn.execute(
                "SELECT value FROM settings WHERE key='invoice_default_due_days'"
            ).fetchone()
            try:
                default_days = max(0, int(setting["value"])) if setting else 0
            except (TypeError, ValueError):
                default_days = 0

        for row in rows:
            try:
                snapshot = json.loads(row.get("print_options") or "{}")
                due_days = max(0, int(snapshot.get("due_days", default_days)))
            except (TypeError, ValueError, json.JSONDecodeError):
                due_days = default_days
            due = invoice_settings.due_date_from_days(row.get("invoice_date"), due_days)
            row["due_date"] = due.isoformat() if due else None
            row["days_overdue"] = max(0, (end - due).days) if due else 0
            row["days_until_due"] = (due - end).days if due else None
            row["due_status"] = (
                "overdue" if due and due < end else
                "due_today" if due and due == end else
                "upcoming" if due else "no_due_date"
            )
        priorities = sorted(
            rows,
            key=lambda row: (
                {"overdue": 0, "due_today": 1, "upcoming": 2, "no_due_date": 3}[row["due_status"]],
                row["due_date"] or "9999-12-31",
                -float(row["remaining_amount"]),
            ),
        )
        return {
            "as_of": end.isoformat(),
            "basis": "configured_due_date",
            "default_due_days": default_days,
            "total_outstanding": sum(float(row["remaining_amount"]) for row in rows),
            "overdue_total": sum(
                float(row["remaining_amount"]) for row in rows if row["due_status"] == "overdue"
            ),
            "invoices": priorities,
        }

    @blocking
    def expense_anomalies(self, *, as_of: str | None = None, months: int = 3) -> dict:
        """Compare month-to-date spend with a day-normalized recent baseline."""
        end = date.fromisoformat(as_of) if as_of else date.today()
        months = max(2, min(int(months), 12))
        month_start = end.replace(day=1)
        baseline_month_index = month_start.year * 12 + month_start.month - 1 - months
        baseline_year, baseline_month_zero = divmod(baseline_month_index, 12)
        baseline_start = date(baseline_year, baseline_month_zero + 1, 1)
        with self.db.connect() as conn:
            current_rows = conn.execute(
                """SELECT COALESCE(ec.name,e.category,'بلا تصنيف') AS category,
                          SUM(e.amount) AS amount
                   FROM expenses e LEFT JOIN expense_categories ec ON ec.id=e.category_id
                   WHERE e.expense_date>=? AND e.expense_date<=?
                   GROUP BY COALESCE(ec.name,e.category,'بلا تصنيف')""",
                (month_start.isoformat(), end.isoformat()),
            ).fetchall()
            history = conn.execute(
                """SELECT substr(e.expense_date,1,7) AS month,
                          COALESCE(ec.name,e.category,'بلا تصنيف') AS category,
                          SUM(e.amount) AS amount
                   FROM expenses e LEFT JOIN expense_categories ec ON ec.id=e.category_id
                   WHERE e.expense_date>=? AND e.expense_date<?
                   GROUP BY substr(e.expense_date,1,7),COALESCE(ec.name,e.category,'بلا تصنيف')""",
                (baseline_start.isoformat(), month_start.isoformat()),
            ).fetchall()

        current = {str(row["category"]): float(row["amount"] or 0) for row in current_rows}
        history_by_category: dict[str, list[float]] = {}
        for row in history:
            category, month = str(row["category"]), str(row["month"])
            history_by_category.setdefault(category, []).append(float(row["amount"] or 0))

        elapsed_days = end.day
        baseline_months = months
        anomalies = []
        for category in set(current) | set(history_by_category):
            current_amount = current.get(category, 0.0)
            monthly_history = history_by_category.get(category, [])
            if not monthly_history:
                continue
            avg_full_month = sum(monthly_history) / baseline_months if monthly_history else 0.0
            expected_to_date = avg_full_month * elapsed_days / 30.0
            difference = current_amount - expected_to_date
            ratio = current_amount / expected_to_date if expected_to_date > 1e-9 else (float("inf") if current_amount > 0 else 0.0)
            if difference >= max(10.0, expected_to_date * 0.5) and ratio >= 1.5:
                anomalies.append({
                    "category": category,
                    "current_month_to_date": current_amount,
                    "expected_to_date": expected_to_date,
                    "difference": difference,
                    "increase_percent": (ratio - 1) * 100 if ratio != float("inf") else None,
                    "baseline_months": baseline_months,
                })
        anomalies.sort(key=lambda row: row["difference"], reverse=True)
        return {
            "as_of": end.isoformat(),
            "elapsed_days": elapsed_days,
            "baseline_months": baseline_months,
            "anomalies": anomalies,
        }

    @blocking
    def cash_commitments(self, *, as_of: str | None = None) -> dict:
        """Summarize dated open invoices; this is not a full cash-flow forecast."""
        end = date.fromisoformat(as_of) if as_of else date.today()
        with self.db.connect() as conn:
            default_row = conn.execute(
                "SELECT value FROM settings WHERE key='invoice_default_due_days'"
            ).fetchone()
            try:
                default_days = max(0, int(default_row["value"])) if default_row else 0
            except (TypeError, ValueError):
                default_days = 0
            invoice_rows = conn.execute(
                """SELECT i.id,i.type,i.invoice_date,i.total,i.paid_amount,i.print_options,
                          COALESCE(c.name,s.name,'') AS party_name
                   FROM invoices i
                   LEFT JOIN customers c ON i.type='sale' AND c.id=i.customer_id
                   LEFT JOIN suppliers s ON i.type='purchase' AND s.id=i.supplier_id
                   WHERE i.invoice_date<=? AND i.total-i.paid_amount>1e-9
                     AND COALESCE(i.notes,'') NOT LIKE ?
                   ORDER BY i.invoice_date,i.id""",
                (end.isoformat(), f"%{OPENING_BALANCE_MARKER}%"),
            ).fetchall()
            cash = float(conn.execute(
                "SELECT COALESCE(SUM(debit-credit),0) FROM ledger_entries "
                "WHERE account_code='CASH' AND entry_date<=?",
                (end.isoformat(),),
            ).fetchone()[0] or 0)

        buckets = {
            "overdue_receivables": 0.0, "overdue_payables": 0.0,
            "next_7_receivables": 0.0, "next_7_payables": 0.0,
            "next_30_receivables": 0.0, "next_30_payables": 0.0,
            "undated_receivables": 0.0, "undated_payables": 0.0,
        }
        due_rows: list[dict] = []
        excluded_undated = 0
        for raw in invoice_rows:
            row = dict(raw)
            try:
                snapshot = json.loads(row.get("print_options") or "{}")
                due_days = max(0, int(snapshot.get("due_days", default_days)))
            except (TypeError, ValueError, json.JSONDecodeError):
                due_days = default_days
            due = invoice_settings.due_date_from_days(row["invoice_date"], due_days)
            remaining = max(0.0, float(row["total"] or 0) - float(row["paid_amount"] or 0))
            if due is None:
                excluded_undated += 1
                buckets["undated_receivables" if row["type"] == "sale" else "undated_payables"] += remaining
                continue
            delta = (due - end).days
            side = "receivables" if row["type"] == "sale" else "payables"
            if delta < 0:
                buckets[f"overdue_{side}"] += remaining
            elif delta <= 7:
                buckets[f"next_7_{side}"] += remaining
            elif delta <= 30:
                buckets[f"next_30_{side}"] += remaining
            due_rows.append({
                "invoice_id": int(row["id"]),
                "invoice_type": str(row["type"]),
                "party_name": str(row["party_name"]),
                "due_date": due.isoformat(),
                "days_until_due": delta,
                "remaining_amount": remaining,
            })
        due_rows.sort(key=lambda row: (row["due_date"], row["invoice_type"], row["invoice_id"]))
        return {
            "as_of": end.isoformat(),
            "book_cash": cash,
            "due_days_policy": default_days,
            "undated_invoice_count": excluded_undated,
            "buckets": buckets,
            "net_overdue": buckets["overdue_receivables"] - buckets["overdue_payables"],
            "net_next_7_days": buckets["next_7_receivables"] - buckets["next_7_payables"],
            "net_next_30_days": (
                buckets["next_7_receivables"] + buckets["next_30_receivables"]
                - buckets["next_7_payables"] - buckets["next_30_payables"]
            ),
            "scenario_only": True,
            "rows": due_rows,
        }

    @blocking
    def cash_movement(self, *, date_from: str | None = None, date_to: str | None = None) -> dict:
        date_from, date_to = self._normalize_dates(date_from, date_to)
        clause, params = self._date_clause("entry_date", date_from, date_to)
        with self.db.connect() as conn:
            opening = 0.0
            if date_from:
                opening = float(conn.execute(
                    "SELECT COALESCE(SUM(debit-credit),0) FROM ledger_entries WHERE account_code='CASH' AND entry_date<?",
                    (date_from,),
                ).fetchone()[0])
            rows = [dict(r) for r in conn.execute(
                f"""SELECT entry_date,debit,credit,source_type,source_id,description
                    FROM ledger_entries
                    WHERE account_code='CASH' AND {clause}
                    ORDER BY entry_date,id""",
                params,
            ).fetchall()]
            receipts = sum(float(r["debit"] or 0) for r in rows)
            payments = sum(float(r["credit"] or 0) for r in rows)
            running = opening
            for row in rows:
                running += float(row["debit"] or 0) - float(row["credit"] or 0)
                row["balance"] = running
            return {
                "date_from": date_from,
                "date_to": date_to,
                "opening_balance": opening,
                "receipts": receipts,
                "payments": payments,
                "net_movement": receipts - payments,
                "closing_balance": running,
                "rows": rows,
            }
