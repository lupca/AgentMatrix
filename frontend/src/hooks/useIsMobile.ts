import { useState, useEffect } from 'react';

function getInitialValue(breakpoint: number): boolean {
  if (typeof window === 'undefined') return false;
  return window.matchMedia(`(max-width: ${breakpoint - 1}px)`).matches;
}

export function useIsMobile(breakpoint = 768) {
  const [isMobile, setIsMobile] = useState<boolean>(() => getInitialValue(breakpoint));

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const mediaQuery = window.matchMedia(`(max-width: ${breakpoint - 1}px)`);
    
    // Set initial value
    setIsMobile(mediaQuery.matches);

    // Create event listener
    const handler = (event: MediaQueryListEvent) => {
      setIsMobile(event.matches);
    };

    // Add event listener
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener('change', handler);
    } else {
      // Fallback for older browsers
      mediaQuery.addListener(handler);
    }

    // Cleanup
    return () => {
      if (mediaQuery.removeEventListener) {
        mediaQuery.removeEventListener('change', handler);
      } else {
        // Fallback for older browsers
        mediaQuery.removeListener(handler);
      }
    };
  }, [breakpoint]);

  return isMobile;
}
