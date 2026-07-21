import { convertBBoxToDisplay, NamedBox } from './auditor-authenticity-detail.component';

// v6 spec Step 6: offline, no AI calls, no network — pure unit tests
// for the single bbox-to-display conversion function, using a minimal
// stub of the DOM properties it reads (clientWidth/clientHeight for
// the DISPLAYED size, naturalWidth/naturalHeight for the image's
// intrinsic pixel size) instead of a real <img> element.

function stubImage(clientWidth: number, clientHeight: number, naturalWidth: number, naturalHeight: number): HTMLImageElement {
  return { clientWidth, clientHeight, naturalWidth, naturalHeight } as HTMLImageElement;
}

function box(overrides: Partial<NamedBox>): NamedBox {
  return {
    type: 'supplier_logo',
    label: 'Test box',
    x: 0, y: 0, width: 0, height: 0,
    confidence: 0.9,
    ...overrides,
  };
}

describe('convertBBoxToDisplay', () => {
  it('scales normalized_0_1 coordinates by the DISPLAYED size only (fractions of the image)', () => {
    const img = stubImage(700, 990, 1785, 2526); // displayed 700x990, natural 1785x2526 (different!)
    const b = box({ coordinate_space: 'normalized_0_1', x: 0.04, y: 0.078, width: 0.3, height: 0.067 });
    const result = convertBBoxToDisplay(b, img);
    expect(result).toEqual({ left: 28, top: 77.22, width: 210, height: 66.33 });
  });

  it('scales normalized_0_1000 coordinates by displayedSize/1000 (Gemini legacy convention)', () => {
    const img = stubImage(700, 990, 1785, 2526);
    const b = box({ coordinate_space: 'normalized_0_1000', x: 40, y: 78, width: 300, height: 67 });
    const result = convertBBoxToDisplay(b, img);
    expect(result?.left).toBeCloseTo(28, 5);
    expect(result?.top).toBeCloseTo(77.22, 5);
    expect(result?.width).toBeCloseTo(210, 5);
    expect(result?.height).toBeCloseTo(66.33, 5);
  });

  it('scales native_pixels coordinates using the box\'s OWN recorded source_image_width/height', () => {
    const img = stubImage(700, 990, 1785, 2526); // currently-loaded image's natural size
    // Box carries a DIFFERENT source size than the currently-loaded image
    // (simulating a box computed against a differently-rendered version) —
    // the box's own recorded dimensions must win, not the image's.
    const b = box({ coordinate_space: 'native_pixels', x: 71, y: 197, width: 535, height: 169, source_image_width: 3570, source_image_height: 5052 });
    const result = convertBBoxToDisplay(b, img);
    // scaleX = 700/3570, scaleY = 990/5052 (the box's OWN source size, NOT the loaded image's natural 1785x2526)
    expect(result?.left).toBeCloseTo(71 * (700 / 3570), 4);
    expect(result?.top).toBeCloseTo(197 * (990 / 5052), 4);
  });

  it('legacy rows with NO coordinate_space at all default to native_pixels against the loaded image\'s natural size (backward compatible)', () => {
    const img = stubImage(700, 990, 1785, 2526);
    const b = box({ x: 40, y: 78, width: 300, height: 67 }); // no coordinate_space, no source_image_width/height
    const result = convertBBoxToDisplay(b, img);
    // scaleX = 700/1785, scaleY = 990/2526
    expect(result?.left).toBeCloseTo(40 * (700 / 1785), 5);
    expect(result?.top).toBeCloseTo(78 * (990 / 2526), 5);
  });

  it('returns null when the image has not finished laying out (zero display or natural dimensions)', () => {
    const b = box({ coordinate_space: 'normalized_0_1', x: 0.1, y: 0.1, width: 0.1, height: 0.1 });
    expect(convertBBoxToDisplay(b, stubImage(0, 990, 1785, 2526))).toBeNull();
    expect(convertBBoxToDisplay(b, stubImage(700, 0, 1785, 2526))).toBeNull();
    expect(convertBBoxToDisplay(b, stubImage(700, 990, 0, 2526))).toBeNull();
    expect(convertBBoxToDisplay(b, stubImage(700, 990, 1785, 0))).toBeNull();
  });

  it('scales correctly across a responsive resize (same box, smaller displayed image)', () => {
    const b = box({ coordinate_space: 'normalized_0_1', x: 0.5, y: 0.5, width: 0.1, height: 0.1 });
    const wide = convertBBoxToDisplay(b, stubImage(1000, 1414, 1785, 2526));
    const narrow = convertBBoxToDisplay(b, stubImage(350, 494.9, 1785, 2526)); // ~35% width (sidebar toggled / narrow viewport)
    expect(wide?.left).toBe(500);
    expect(narrow?.left).toBe(175);
    // Aspect ratio (width/height) of the box itself is preserved across the resize.
    expect(wide!.width / wide!.height).toBeCloseTo(narrow!.width / narrow!.height, 5);
  });
});
