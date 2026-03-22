# Bet The Farm Dashboard - Update Guide

## Current Status (Mar 1, 2026 4:02 AM)

✅ **Header Timestamp**: Updated to current date/time
✅ **Data Arrays**: All 6 sports loaded (NFL, CFB, NBA, CBB, NHL, MLB)
✅ **JavaScript Syntax**: Validated and working
✅ **Backup**: Original file backed up

## Data Array Structure

Each sport has a JavaScript array with team data. The structure is as follows:

### NFL Data Array Format
```javascript
const NFL=[
  ["Team Name", "Conference", "Division", W, L, atsW, atsL, atsP, hAtsW, hAtsL, aAtsW, aAtsL, over, under, ouP, ppg, papg, offRk, defRk],
  ...
]
```

**Field Indices:**
- 0: Team Name
- 1: Conference (AFC/NFC)
- 2: Division
- 3: Wins (W)
- 4: Losses (L)
- 5: ATS Wins
- 6: ATS Losses
- 7: ATS Push
- 8: Home ATS Wins
- 9: Home ATS Losses
- 10: Away ATS Wins
- 11: Away ATS Losses
- 12: Over Count
- 13: Under Count
- 14: Over/Under Push
- 15: PPG (Points Per Game)
- 16: PAPG (Points Against Per Game)
- 17: Offensive Ranking
- 18: Defensive Ranking

### CFB Data Array Format
**Field Indices:**
- 0: Rank
- 1: Team Name
- 2: Conference
- 3: Wins (W)
- 4: Losses (L)
- 5: ATS Wins
- 6: ATS Losses
- 7: ATS%
- 8: Home ATS W-L
- 9: Away ATS W-L
- 10: Over Count
- 11: Under Count
- 12: SP+ Offense
- 13: SP+ Defense
- 14: PPG
- 15: PAPG

### NBA/MLB Data Format (Similar to NFL)
Same structure as NFL with appropriate stat columns.

### CBB Data Format
- 0: Rank
- 1: Team Name
- 2: Conference
- 3: Wins (W)
- 4: Losses (L)
- 5: ATS Wins
- 6: ATS Losses
- 7: Home ATS W-L
- 8: Away ATS W-L
- 9: Over Count
- 10: Under Count
- 11: AdjOE
- 12: AdjDE
- 13: AdjEM
- 14: Tempo
- 15: PPG
- 16: PAPG

### NHL Data Format
- 0: Team Name
- 1: Conference
- 2: Division
- 3: Wins
- 4: Losses
- 5: OT Losses
- 6: Puck Line Push
- 7: Puck Line Wins
- 8: Puck Line Losses
- 9: Home W-L
- 10: Away W-L
- 11: Over Count
- 12: Under Count
- 13: Goals For
- 14: Goals Against
- 15: PP%
- 16: PK%

## How to Update Data

### Option 1: Manual Update (Most Accurate)

1. **For ATS Data:**
   - Visit: https://www.teamrankings.com/nfl/trends/ats_trends/
   - Replace `/nfl/` with `/cfb/`, `/nba/`, `/cbb/`, `/nhl/`, or `/mlb/`
   - Screenshot or note the ATS records (W-L) for each team

2. **For W/L Standings:**
   - Visit: https://www.espn.com/nfl/standings
   - Replace `/nfl/` with appropriate sport
   - Note current season W-L records

3. **Update the Arrays:**
   - Open this HTML file in a text editor
   - Find the data array for the sport (e.g., `const NFL=[`)
   - Update only the numeric fields (indices 3-13)
   - Do NOT change team names or other non-numeric data
   - Keep the array syntax intact

### Option 2: Programmatic Update

A Python script has been provided at: `/tmp/comprehensive_updater.py`

This script can:
- Parse team data from CSV/JSON formats
- Match team names with fuzzy matching
- Update only the specified fields
- Validate JavaScript syntax
- Create backups automatically

### Critical Fields to Update

**Always Update These Indices:**
- **Index 3**: Wins
- **Index 4**: Losses
- **Index 5**: ATS Wins (or leave blank if using custom formula)
- **Index 6**: ATS Losses
- **Index 12**: Over Count
- **Index 13**: Under Count

**Do NOT Change:**
- Team names (Index 0)
- Conference/Division (Indices 1-2)
- PPG and defensive stats (vary by sport)
- Array syntax or structure

## Data Sources

### Primary Sources:
1. **ATS Records**: https://www.teamrankings.com/[sport]/trends/ats_trends/
2. **W/L Standings**: https://www.espn.com/[sport]/standings
3. **Advanced Stats**:
   - NFL: Pro Football Reference
   - CFB: ESPN SP+ Ratings
   - NBA: Basketball Reference
   - CBB: KenPom or Bart Torvik
   - NHL: Hockey Reference
   - MLB: Baseball Reference

### Data Frequency:
- Update at least once daily during season
- More frequently during heavy betting periods
- Check before weekend games

## Validation

After updating, always verify:

1. **JavaScript Syntax:**
   ```bash
   node -e "const fs=require('fs');const h=fs.readFileSync('Bet The Farm Hub.html','utf8');const m=h.match(/<script>([\s\S]*?)<\/script>/);try{new Function(m[1]);console.log('OK')}catch(e){console.log('ERROR:',e.message)}"
   ```

2. **File Integrity:**
   - Backup exists
   - File size reasonable (>100KB)
   - All table IDs present (nfl-tbl, cfb-tbl, etc.)

3. **Data Consistency:**
   - Team count matches expected (32 NFL, 30 NBA/NHL/MLB, etc.)
   - No duplicate teams
   - No empty fields for required data

## Troubleshooting

**Problem:** JavaScript syntax errors after update
**Solution:** Check for missing commas, unclosed brackets, or quotes in array data

**Problem:** Data not displaying in browser
**Solution:** Verify all team names exactly match the original (case-sensitive)

**Problem:** Team not found in dropdown
**Solution:** Check team name spelling and order - must match data array

**Problem:** Numbers not updating in table
**Solution:** Verify indices are correct for the sport (indices differ between sports)

## Backup and Recovery

- **Backup location**: Same directory as original file with `.backup` extension
- **To restore**: Copy `.backup` file over original and refresh browser
- **Backup frequency**: Created automatically before each update

## Notes

- The dashboard uses client-side JavaScript - no server needed
- All data is embedded in the HTML file
- Updates take effect immediately after file save and browser refresh
- No authentication or API keys required

---
Last Updated: Mar 1, 2026 4:02 AM
