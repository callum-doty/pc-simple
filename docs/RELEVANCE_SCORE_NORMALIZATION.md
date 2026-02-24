# Relevance Score Normalization

## Overview

Implemented min-max normalization for search relevance scores to better utilize the full (0, 1) range and improve score distribution.

## Problem Statement

The hybrid search system combines vector search (70% weight) and text search (30% weight) using this formula:

```python
relevance = (vector_relevance * 0.7) + (text_relevance * 0.3)
```

However, in practice, scores were clustering in the 0.3-0.5 range and never exceeding 0.50, which meant:
- The full (0, 1) range wasn't being utilized
- Score differentiation between documents was poor
- Users couldn't easily identify the "best" matches

## Root Cause

The issue stemmed from the natural distribution of similarity scores:

1. **Vector Cosine Similarity**: Even good matches typically have cosine similarity in the 0.6-0.8 range (not 0.9-1.0)
2. **Text Ranking**: PostgreSQL's `ts_rank_cd` typically returns values between 0.1-1.5 for normal documents
3. **Combined Effect**: 
   - Vector: 0.7 × 0.7 = 0.49
   - Text: 0.3 × 1.5 = 0.45
   - Total: ~0.45-0.50 max score

This meant that even perfect matches were only scoring around 0.5, leaving the upper half of the scale unused.

## Solution: Min-Max Normalization

Applied **min-max normalization** to the relevance scores within each result set:

```python
normalized_score = (score - min_score) / (max_score - min_score)
```

### Implementation Details

1. **Collect scores**: After query execution, gather all relevance scores from the result set
2. **Calculate range**: Find the minimum and maximum scores
3. **Normalize**: Scale all scores to fit the (0, 1) range
4. **Apply**: The highest-scoring document gets 1.0, lowest gets 0.0, others are proportionally distributed

### Code Location

File: `services/search_service.py`, in the `search()` method, between steps 5 and 6.

```python
# 6. Normalize relevance scores to better utilize (0, 1) range
relevance_scores = [relevance for _, relevance in results]

if relevance_scores and any(score > 0 for score in relevance_scores):
    min_score = min(relevance_scores)
    max_score = max(relevance_scores)
    
    if max_score > min_score:
        normalized_results = []
        for doc, relevance in results:
            normalized_score = (relevance - min_score) / (max_score - min_score)
            normalized_results.append((doc, normalized_score))
        results = normalized_results
```

## Benefits

### 1. **Full Range Utilization**
- Best match: 1.00
- Worst match: 0.00
- Clear visual differentiation

### 2. **Relative Ranking Preserved**
- Original order is maintained
- Only the scale changes, not the ranking

### 3. **Better User Experience**
- Easy to identify top results (scores near 1.0)
- Clear distinction between good and poor matches
- More intuitive score interpretation

### 4. **Query-Adaptive**
- Normalization happens per result set
- Adapts to the quality of matches for each specific query
- No fixed thresholds that might not fit all queries

## Example Comparison

### Before Normalization:
```
Document A: 0.48
Document B: 0.45
Document C: 0.42
Document D: 0.38
Document E: 0.35
```
*Range: 0.35-0.48 (only 13% of scale used)*

### After Normalization:
```
Document A: 1.00  ← Best match
Document B: 0.77
Document C: 0.54
Document D: 0.23
Document E: 0.00  ← Worst match
```
*Range: 0.00-1.00 (full scale utilized)*

## Trade-offs

### Advantages ✅
- Clearer score differentiation
- Better use of visual indicators (badges, colors)
- More intuitive for users
- Adaptive to query quality

### Considerations ⚠️
- Scores are **relative within each result set**, not absolute across queries
- A score of 0.8 on Query A might represent a different quality than 0.8 on Query B
- Cannot directly compare scores across different searches
- A query with all poor matches will still show scores from 0-1

## Performance Impact

- **Minimal overhead**: O(n) operation where n = results per page (typically 20)
- **No database impact**: Normalization happens in Python after query execution
- **No cache issues**: Normalization occurs before caching, so cached results are already normalized

## Testing Recommendations

1. **Run diverse queries** to verify score distribution
2. **Check edge cases**:
   - Single result (no normalization possible)
   - All identical scores (no normalization applied)
   - Empty results
3. **Compare rankings** before/after to ensure order is preserved
4. **Monitor user feedback** on score interpretability

## Alternative Approaches Considered

### 1. Adjust Weights (Not Chosen)
- Change from 70/30 to 50/50
- **Rejected**: Would reduce vector search importance

### 2. Fixed Scaling Factor (Not Chosen)
- Multiply all scores by 2
- **Rejected**: Could exceed 1.0, doesn't adapt to query quality

### 3. Sigmoid Normalization (Not Chosen)
- Apply sigmoid function: 1/(1+e^-x)
- **Rejected**: More complex, harder to interpret

### 4. Min-Max Normalization (✅ Chosen)
- Simple, interpretable, adaptive
- Preserves ranking while maximizing differentiation

## Future Enhancements

Potential improvements to consider:

1. **Global Score Calibration**: Track score distributions across all queries to provide absolute scoring
2. **Score Quality Indicator**: Show users whether the overall match quality is high/medium/low
3. **Percentile Ranks**: Show "Top 10%" style indicators
4. **A/B Testing**: Compare user satisfaction with normalized vs. raw scores

## Related Documentation

- `docs/10K_DOCUMENT_OPTIMIZATION.md` - Vector search optimization
- `architecture/database_schema.md` - HNSW index configuration
- `services/search_service.py` - Search implementation

## Implementation Date

February 23, 2026

## Author

System update implementing user-requested feature for better score distribution.
