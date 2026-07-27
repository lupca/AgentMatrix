import React from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

export interface PaginationProps {
  currentPage: number;
  totalPages: number;
  totalItems: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
}

const PAGE_SIZE_OPTIONS = [10, 25, 50];

export const Pagination: React.FC<PaginationProps> = ({
  currentPage,
  totalPages,
  totalItems,
  pageSize,
  onPageChange,
  onPageSizeChange,
}) => {
  const firstPage = 1;
  const lastPage = Math.max(firstPage, totalPages);
  const page = Math.min(Math.max(currentPage, firstPage), lastPage);

  return (
    <div className="flex flex-col sm:flex-row items-center justify-between gap-3 pt-4 text-xs">
      <span className="text-gray-400">
        Page {page} of {lastPage} ({totalItems} items)
      </span>

      <div className="flex items-center gap-2">
        {onPageSizeChange && (
          <label className="flex items-center gap-2 text-gray-400">
            <span>Rows:</span>
            <select
              value={pageSize}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
              className="bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-300 focus:outline-none focus:border-indigo-500"
              aria-label="Rows per page"
            >
              {PAGE_SIZE_OPTIONS.map((size) => (
                <option key={size} value={size}>{size}</option>
              ))}
            </select>
          </label>
        )}

        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={page === firstPage}
          className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed"
          aria-label="Previous page"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={page === lastPage}
          className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed"
          aria-label="Next page"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
};

export default Pagination;
