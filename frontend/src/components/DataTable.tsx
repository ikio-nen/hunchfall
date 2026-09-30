import type { ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: string;
  /** Render the cell value; receives the row. */
  render: (row: T) => ReactNode;
  /** Optional CSS class for the cell (e.g. numeric alignment). */
  className?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  /** Stable unique key per row, for React keys. */
  rowKey: (row: T, i: number) => string;
  emptyText?: string;
}

export default function DataTable<T>({
  columns,
  rows,
  rowKey,
  emptyText = "No rows.",
}: DataTableProps<T>) {
  if (rows.length === 0) return <div className="empty">{emptyText}</div>;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey(row, i)}>
              {columns.map((c) => (
                <td key={c.key} className={c.className ?? ""}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
