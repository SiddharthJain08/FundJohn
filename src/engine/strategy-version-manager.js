/**
 * Strategy Version Manager
 *
 * Strategies are immutable once deployed. This module manages the version
 * lifecycle:
 *   - Each strategy file is write-protected after deployment
 *   - Parameter adjustments create a new versioned file
 *   - Only one version of each strategy is active at a time
 *   - The original file is never modified or deleted
 *
 * Versioned file naming:
 *   Original:  strategies/mv01_momentum_value.py        (v1, written once)
 *   Adjusted:  strategies/mv01_momentum_value_v2.py     (new file for v2)
 *   Adjusted:  strategies/mv01_momentum_value_v3.py     (new file for v3)
 *
 * The STRATEGY_ID inside each file always reflects the versioned ID:
 *   v1:  STRATEGY_ID = 'MV01_momentum_value_v1'
 *   v2:  STRATEGY_ID = 'MV01_momentum_value_v2'
 */

const fs   = require('fs');
const path = require('path');
const { Pool } = require('pg');
const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

const STRATEGIES_DIR = 'workspaces/default/strategies';

/**
 * Lock a file as read-only after deployment.
 * This is a soft lock — it prevents accidental overwrites, not adversarial edits.
 */
function lockFile(filePath) {
    try {
        fs.chmodSync(filePath, 0o444);  // read-only for all
        return true;
    } catch (e) {
        console.warn(`Could not lock ${filePath}: ${e.message}`);
        return false;
    }
}

/**
 * Register a strategy version.
 * Called once per deployment (including the initial deployment of v1).
 */
async function registerVersion({
    workspaceId,
    strategyId,          // base ID: 'MV01_momentum_value'
    versionedId,         // versioned ID: 'MV01_momentum_value_v1'
    filePath,
    params,
    paramChanges,
    changeReason,
    validationPassed,
    validationReport,
    deployedBy = 'operator',
}) {
    // Get next version number
    const existing = await pool.query(
        `SELECT MAX(version) as max_v FROM strategy_versions
         WHERE strategy_id = $1 AND workspace_id = $2`,
        [strategyId, workspaceId]
    );
    const nextVersion = (existing.rows[0].max_v || 0) + 1;

    // Deactivate previous version
    await pool.query(
        `UPDATE strategy_versions
         SET is_active = FALSE, superseded_at = NOW(), superseded_by = $1
         WHERE strategy_id = $2 AND workspace_id = $3 AND is_active = TRUE`,
        [versionedId, strategyId, workspaceId]
    );

    // Insert new version
    await pool.query(
        `INSERT INTO strategy_versions
           (workspace_id, strategy_id, version, versioned_id, file_path,
            params, param_changes, change_reason, validation_passed,
            validation_report, is_active, deployed_by)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,TRUE,$11)`,
        [workspaceId, strategyId, nextVersion, versionedId, filePath,
         JSON.stringify(params || {}), JSON.stringify(paramChanges || {}),
         changeReason, validationPassed, JSON.stringify(validationReport || {}),
         deployedBy]
    );

    // Register versioned ID in strategy_registry under the versioned ID
    // Get name and tier from the base strategy entry
    const baseEntry = await pool.query(
        `SELECT name, tier, implementation_path FROM strategy_registry WHERE id = $1`,
        [strategyId]
    );

    if (baseEntry.rows.length > 0) {
        const base = baseEntry.rows[0];
        const versionedName = `${base.name} v${nextVersion}`;
        await pool.query(
            `INSERT INTO strategy_registry
               (id, name, tier, implementation_path, status)
             VALUES ($1, $2, $3, $4, 'active')
             ON CONFLICT (id) DO UPDATE
               SET status = 'active', implementation_path = EXCLUDED.implementation_path`,
            [versionedId, versionedName, base.tier, filePath]
        );

        // Deactivate previous versioned entries for this base strategy
        await pool.query(
            `UPDATE strategy_registry
             SET status = 'deprecated'
             WHERE id != $1
               AND id != $2
               AND id LIKE $3`,
            [versionedId, strategyId, `${strategyId}%`]
        );
    }

    // Lock the file
    lockFile(path.resolve(filePath));

    console.log(`Strategy version registered: ${versionedId} (v${nextVersion})`);
    return { versionedId, version: nextVersion };
}

/**
 * Create a new version of an existing strategy with adjusted parameters.
 * Reads the active version's source file, creates a copy with new STRATEGY_ID
 * and new parameter defaults, then registers it.
 *
 * The original file is NEVER modified.
 */
async function createNewVersion(workspaceId, baseStrategyId, newParams, changeReason) {
    // Get current active version
    const current = await pool.query(
        `SELECT * FROM strategy_versions
         WHERE strategy_id = $1 AND workspace_id = $2 AND is_active = TRUE`,
        [baseStrategyId, workspaceId]
    );

    if (current.rows.length === 0) {
        throw new Error(`No active version found for strategy: ${baseStrategyId}`);
    }

    const currentRow  = current.rows[0];
    const nextVersion = currentRow.version + 1;
    const newVersionedId = `${baseStrategyId}_v${nextVersion}`;

    // Read current strategy source (need to temporarily allow read of locked file)
    const currentSource = fs.readFileSync(currentRow.file_path, 'utf8');

    // Compute parameter diff
    const currentParams = currentRow.params || {};
    const paramChanges  = {};
    for (const [key, value] of Object.entries(newParams)) {
        if (currentParams[key] !== value) {
            paramChanges[key] = { from: currentParams[key], to: value };
        }
    }

    if (Object.keys(paramChanges).length === 0) {
        throw new Error('No parameter changes detected. New version not created.');
    }

    // Create new file path
    const ext         = path.extname(currentRow.file_path);
    const baseName    = path.basename(currentRow.file_path, ext);
    // Strip any existing _v suffix before adding new one
    const cleanBase   = baseName.replace(/_v\d+$/, '');
    const newFileName = `${cleanBase}_v${nextVersion}${ext}`;
    const newFilePath = path.join(STRATEGIES_DIR, newFileName);

    // Write new file: update STRATEGY_ID and apply new parameter defaults
    let newSource = currentSource;

    // Update STRATEGY_ID in the file
    newSource = newSource.replace(
        /STRATEGY_ID\s*=\s*['"][^'"]+['"]/,
        `STRATEGY_ID = '${newVersionedId}'`
    );

    // Apply parameter overrides (update class-level constants)
    for (const [key, value] of Object.entries(newParams)) {
        const constName = key.toUpperCase();
        const pattern   = new RegExp(`(${constName}\\s*=\\s*)[^\\n]+`);
        if (pattern.test(newSource)) {
            const formatted = typeof value === 'string' ? `'${value}'` : JSON.stringify(value);
            newSource = newSource.replace(pattern, `$1${formatted}`);
        }
    }

    // Write the new file
    fs.writeFileSync(newFilePath, newSource, { mode: 0o644 });

    console.log(`New strategy file created: ${newFilePath}`);

    // Register the new version
    const result = await registerVersion({
        workspaceId,
        strategyId:       baseStrategyId,
        versionedId:      newVersionedId,
        filePath:         newFilePath,
        params:           { ...currentParams, ...newParams },
        paramChanges,
        changeReason,
        validationPassed: true,  // inherited validation from original
        validationReport: { note: 'Parameter adjustment — inherits validation from original' },
        deployedBy:       'operator',
    });

    return {
        ...result,
        newFilePath,
        paramChanges,
        previousVersion: currentRow.versioned_id,
    };
}

/**
 * Get version history for a strategy.
 */
async function getVersionHistory(workspaceId, baseStrategyId) {
    const result = await pool.query(
        `SELECT version, versioned_id, params, param_changes, change_reason,
                deployed_at, is_active, signal_count, validation_passed
         FROM strategy_versions
         WHERE strategy_id = $1 AND workspace_id = $2
         ORDER BY version ASC`,
        [baseStrategyId, workspaceId]
    );
    return result.rows;
}

/**
 * Register the three base strategies as v1 (pre-verified, no validation needed).
 * Called once during system setup.
 */
async function registerBaseStrategies(workspaceId) {
    const BASE_STRATEGIES = [
        {
            strategyId:  'MV01_momentum_value',
            versionedId: 'MV01_momentum_value_v1',
            filePath:    `${STRATEGIES_DIR}/mv01_momentum_value.py`,
            params: {
                LONG_THRESHOLD:  0.80,
                SHORT_THRESHOLD: 0.20,
                MIN_HISTORY:     252,
            },
            notes: 'Base strategy v1 — pre-verified, no validation required',
        },
        {
            strategyId:  'CA02_cross_asset',
            versionedId: 'CA02_cross_asset_v1',
            filePath:    `${STRATEGIES_DIR}/ca02_cross_asset.py`,
            params: {
                LOOKBACK_Z: 20,
                SIGNAL_Z:   1.5,
                MIN_AGREE:  2,
            },
            notes: 'Base strategy v1 — pre-verified, no validation required',
        },
        {
            strategyId:  'BS03_options_mispricing',
            versionedId: 'BS03_options_mispricing_v1',
            filePath:    `${STRATEGIES_DIR}/bs03_options_mispricing.py`,
            params: {
                MIN_MISPRICING_PCT: 0.15,
                MIN_IV_HV_DISCOUNT: 0.10,
                MIN_DTE:            14,
                MAX_DTE:            45,
                MAX_MONEYNESS:      0.05,
            },
            notes: 'Base strategy v1 — pre-verified, no validation required',
        },
    ];

    for (const s of BASE_STRATEGIES) {
        if (!fs.existsSync(s.filePath)) {
            console.warn(`Base strategy file not found: ${s.filePath} — skipping`);
            continue;
        }

        // Check if already registered
        const existing = await pool.query(
            `SELECT id FROM strategy_versions WHERE versioned_id = $1`,
            [s.versionedId]
        );
        if (existing.rows.length > 0) {
            console.log(`Already registered: ${s.versionedId}`);
            continue;
        }

        await registerVersion({
            workspaceId,
            strategyId:       s.strategyId,
            versionedId:      s.versionedId,
            filePath:         s.filePath,
            params:           s.params,
            paramChanges:     {},
            changeReason:     'Initial deployment — base strategy',
            validationPassed: true,
            validationReport: { note: s.notes },
            deployedBy:       'system',
        });
    }

    console.log('Base strategies registered as v1.');
}

module.exports = {
    registerVersion,
    createNewVersion,
    getVersionHistory,
    registerBaseStrategies,
    lockFile,
};
